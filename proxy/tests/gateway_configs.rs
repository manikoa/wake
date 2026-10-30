//! The agentgateway configs that ship with Wake keep the deny lever whole: every call is
//! checked at the gateway, before routing, and fails closed; every A2A route also names the
//! agent it reaches; and callers authenticate, so the deny knows which agent is calling.
//! A route added without these would let a halted agent's calls through.

use yaml_rust2::{Yaml, YamlLoader};

const CONFIGS: [(&str, Auth); 3] = [
    ("deploy/agentgateway.yaml", Auth::Required),
    ("proxy/agentgateway.yaml", Auth::Required),
    // The reference fleet names callers from a header any caller can set, so it needs no
    // identity provider; an agent halt there denies callers it can't name.
    ("examples/fleet/agentgateway.yaml", Auth::StandIn),
];

#[derive(Clone, Copy, PartialEq)]
enum Auth {
    Required,
    StandIn,
}

fn load(path: &str) -> Yaml {
    let full = format!("{}/../{path}", env!("CARGO_MANIFEST_DIR"));
    let text = std::fs::read_to_string(&full).unwrap_or_else(|e| panic!("{full}: {e}"));
    YamlLoader::load_from_str(&text)
        .unwrap_or_else(|e| panic!("{path}: {e}"))
        .remove(0)
}

fn entries(y: &Yaml) -> Vec<(String, &Yaml)> {
    y.as_hash()
        .map(|h| {
            h.iter()
                .map(|(k, v)| (k.as_str().unwrap_or_default().to_string(), v))
                .collect()
        })
        .unwrap_or_default()
}

/// What's wrong with an extAuthz policy that should be the deny lever.
fn deny_lever(policy: &Yaml) -> Vec<String> {
    let mut wrong = vec![];
    if policy.is_badvalue() {
        return vec!["no extAuthz".into()];
    }
    if policy["failureMode"].as_str() != Some("deny") {
        wrong.push("extAuthz must fail closed (failureMode: deny)".into());
    }
    if policy["host"]
        .as_str()
        .is_none_or(|h| !h.contains("9001") && !h.starts_with("unix:"))
    {
        wrong.push("extAuthz must call wake-proxy".into());
    }
    if !policy["cache"].is_badvalue() {
        wrong.push("extAuthz mustn't cache: a halt would wait for the cache to expire".into());
    }
    if policy["protocol"]["grpc"]["metadata"]["wake"].as_str().is_none() {
        wrong.push("extAuthz must send the caller in the wake metadata".into());
    }
    wrong
}

#[test]
fn every_call_is_checked_at_the_gateway_and_fails_closed() {
    for (path, auth) in CONFIGS {
        let config = load(path);
        let gateways = entries(&config["gateways"]);
        assert!(!gateways.is_empty(), "{path}: no gateways");
        for (name, gateway) in gateways {
            let wrong = deny_lever(&gateway["extAuthz"]);
            assert!(wrong.is_empty(), "{path}: gateway {name}: {wrong:?}");
            if auth == Auth::Required {
                let mtls = gateway["tls"]["root"].as_str().is_some();
                let jwt = gateway["jwtAuth"]["mode"].as_str() == Some("strict");
                assert!(
                    mtls || jwt,
                    "{path}: gateway {name} must authenticate every caller: tls.root (mTLS) or jwtAuth with mode: strict"
                );
            }
        }
    }
}

#[test]
fn every_a2a_route_denies_calls_to_a_halted_agent() {
    for (path, _) in CONFIGS {
        let config = load(path);
        let routes = config["routes"].as_vec().unwrap_or_else(|| panic!("{path}: no routes"));
        for (i, route) in routes.iter().enumerate() {
            let policies = &route["policies"];
            let name = route["name"].as_str().map(String::from).unwrap_or(format!("#{i}"));
            let authz = &policies["extAuthz"];
            if !authz.is_badvalue() {
                let wrong = deny_lever(authz);
                assert!(wrong.is_empty(), "{path}: route {name}: {wrong:?}");
            }
            if policies["a2a"].is_badvalue() {
                continue;
            }
            let receiver = authz["protocol"]["grpc"]["context"]["receiver"].as_str();
            assert!(
                receiver.is_some_and(|r| !r.is_empty()),
                "{path}: A2A route {name} must name the agent it reaches in extAuthz's context.receiver"
            );
            // The record names the same agent.
            let recorded = policies["extProc"]["requestAttributes"]["receiver"].as_str();
            assert_eq!(
                recorded.map(|r| r.trim_matches('"')),
                receiver,
                "{path}: route {name}: extProc's receiver"
            );
        }
    }
}

#[test]
fn mcp_capture_can_pair_each_response_with_its_request() {
    for (path, _) in CONFIGS {
        let config = load(path);
        for route in config["routes"].as_vec().unwrap() {
            let Some(processors) = route["policies"]["mcpGuardrails"]["processors"].as_vec() else {
                continue;
            };
            for processor in processors {
                let metadata = &processor["metadata"];
                for key in ["agent", "wake_event_id", "session"] {
                    assert!(
                        metadata[key].as_str().is_some(),
                        "{path}: mcpGuardrails metadata needs {key}"
                    );
                }
            }
        }
    }
}

/// What one hook is told about its caller: who it is, and what its JWT says.
struct Hook {
    at: String,
    caller: String,
    token: String,
}

/// Splits the deny lever's `wake` metadata, `{"agent": <caller>, "token": <token>}`.
fn wake_metadata(path: &str, at: &str, w: &str) -> (String, String) {
    w.trim()
        .strip_prefix(r#"{"agent":"#)
        .and_then(|w| w.strip_suffix('}'))
        .and_then(|w| w.split_once(r#", "token":"#))
        .map(|(caller, token)| (caller.trim().to_string(), token.trim().to_string()))
        .unwrap_or_else(|| panic!("{path}: {at}: extAuthz must send {{\"agent\": <caller>, \"token\": <token>}}"))
}

/// Every hook a config gives wake-proxy, with the caller and token expressions it gets: the
/// deny lever's, at the gateway and on each route, ext_proc's sender and ExtMCP's agent.
fn hooks(path: &str, config: &Yaml) -> Vec<Hook> {
    let mut out = vec![];
    let mut wake = |at: String, authz: &Yaml| {
        if let Some(w) = authz["protocol"]["grpc"]["metadata"]["wake"].as_str() {
            let (caller, token) = wake_metadata(path, &at, w);
            out.push(Hook { at, caller, token });
        }
    };
    for (name, gateway) in entries(&config["gateways"]) {
        wake(format!("gateway {name}"), &gateway["extAuthz"]);
    }
    let routes = config["routes"].as_vec().unwrap();
    for (i, route) in routes.iter().enumerate() {
        wake(format!("route #{i} extAuthz"), &route["policies"]["extAuthz"]);
    }
    let text = |y: &Yaml| y.as_str().unwrap_or_default().trim().to_string();
    for (i, route) in routes.iter().enumerate() {
        let policies = &route["policies"];
        let attributes = &policies["extProc"]["requestAttributes"];
        if !attributes["sender"].is_badvalue() {
            out.push(Hook {
                at: format!("route #{i} extProc"),
                caller: text(&attributes["sender"]),
                token: text(&attributes["sender_token"]),
            });
        }
        for processor in policies["mcpGuardrails"]["processors"].as_vec().into_iter().flatten() {
            out.push(Hook {
                at: format!("route #{i} mcpGuardrails"),
                caller: text(&processor["metadata"]["agent"]),
                token: text(&processor["metadata"]["token"]),
            });
        }
    }
    out
}

/// A halt that lands during a call is enforced by the capture hooks: ext_proc cuts an A2A
/// exchange, and ExtMCP keeps a result from its caller. So they must see the caller as the
/// deny lever does, or a halt would cut other agents' calls, or miss the halted one's.
#[test]
fn the_capture_hooks_see_the_caller_the_deny_lever_does() {
    for (path, _) in CONFIGS {
        let config = load(path);
        let gateway = &config["gateways"]["default"]["extAuthz"]["protocol"]["grpc"]["metadata"]["wake"];
        let (caller, _) = wake_metadata(path, "the gateway", gateway.as_str().unwrap_or_default());
        for hook in hooks(path, &config) {
            assert_eq!(hook.caller, caller, "{path}: {}'s caller", hook.at);
        }
    }
}

/// A token from OAuth token exchange (RFC 8693) names whoever it acts for in `sub`, often
/// a user or the agent that delegated, and the agent acting now in `act.sub`. Every hook must
/// name the caller by the agent acting, or a halt on a sub-agent holding a delegated token
/// would never match its calls, and its calls would be recorded as its user's.
#[test]
fn a_delegated_token_names_the_agent_acting() {
    const ACTOR: &str = "has(jwt.act) && has(jwt.act.sub) ? jwt.act.sub : has(jwt.sub) ? jwt.sub :";
    for (path, _) in CONFIGS {
        let all = hooks(path, &load(path));
        assert!(!all.is_empty(), "{path}: no hooks");
        for hook in &all {
            assert!(
                hook.caller.starts_with("has(source.spiffeId) ? source.spiffeId :"),
                "{path}: {}: an SVID comes first: {}",
                hook.at,
                hook.caller
            );
            assert!(
                hook.caller.contains(ACTOR),
                "{path}: {}: a delegated token's act.sub must come before its sub: {}",
                hook.at,
                hook.caller
            );
        }
    }
}

/// Every hook is told what the caller's JWT says, the same way, so a call's record and its
/// denial carry the same facts. Only these claims, never the token: wake-proxy records what
/// it's given (proxy/src/token.rs), and a bearer token in the record could be used by anyone
/// who can read the log.
#[test]
fn every_hook_is_told_what_the_token_says_and_never_the_token() {
    const TOKEN: &str = r#"{"sub": has(jwt.sub) ? jwt.sub : "", "jti": has(jwt.jti) ? jwt.jti : "", "uti": has(jwt.uti) ? jwt.uti : "", "exp": has(jwt.exp) ? jwt.exp : 0, "act": has(jwt.act) ? jwt.act : {}, "cnf": has(jwt.cnf)}"#;
    for (path, _) in CONFIGS {
        for hook in hooks(path, &load(path)) {
            assert_eq!(hook.token, TOKEN, "{path}: {}'s token", hook.at);
        }
        let text = std::fs::read_to_string(format!("{}/../{path}", env!("CARGO_MANIFEST_DIR"))).unwrap();
        for credential in ["authorization", "Authorization", "jwt.raw", "jwt.token"] {
            assert!(!text.contains(credential), "{path} passes {credential} to wake-proxy");
        }
    }
}

/// Every host a config calls wake-proxy's hooks on.
fn hook_hosts(config: &Yaml) -> Vec<String> {
    let mut hosts = vec![];
    let mut add = |y: &Yaml| hosts.extend(y["host"].as_str().map(String::from));
    for (_, gateway) in entries(&config["gateways"]) {
        add(&gateway["extAuthz"]);
        add(&gateway["extProc"]);
    }
    for route in config["routes"].as_vec().unwrap() {
        let policies = &route["policies"];
        add(&policies["extAuthz"]);
        add(&policies["extProc"]);
        for processor in policies["mcpGuardrails"]["processors"].as_vec().into_iter().flatten() {
            add(processor);
        }
    }
    hosts
}

#[test]
fn only_agentgateway_reaches_the_hooks() {
    for (path, _) in CONFIGS {
        let hosts = hook_hosts(&load(path));
        assert!(!hosts.is_empty(), "{path}");
        for host in &hosts {
            // wake-proxy refuses to listen anywhere else (listen.rs).
            let private =
                host.starts_with("unix:/") || host.starts_with("localhost:") || host.starts_with("127.0.0.1:");
            assert!(private, "{path}: hooks on {host}, which other services could reach");
        }
        // The compose stack gives the socket a volume only agentgateway shares.
        if !path.starts_with("proxy/") {
            assert!(
                hosts.iter().all(|h| h == "unix:/run/wake/proxy.sock"),
                "{path}: {hosts:?}"
            );
        }
    }
}
