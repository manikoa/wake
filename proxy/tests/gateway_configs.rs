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

/// A halt that lands during a call is enforced by the capture hooks: ext_proc cuts an A2A
/// exchange, and ExtMCP keeps a result from its caller. So they must see the caller as the
/// deny lever does, or a halt would cut other agents' calls, or miss the halted one's.
#[test]
fn the_capture_hooks_see_the_caller_the_deny_lever_does() {
    for (path, _) in CONFIGS {
        let config = load(path);
        let gateway = &config["gateways"]["default"]["extAuthz"]["protocol"]["grpc"]["metadata"]["wake"];
        let caller = gateway
            .as_str()
            .and_then(|w| w.trim().strip_prefix(r#"{"agent":"#))
            .and_then(|w| w.strip_suffix('}'))
            .map(str::trim)
            .unwrap_or_else(|| panic!("{path}: the gateway's extAuthz must send {{\"agent\": <caller>}}"));
        for (i, route) in config["routes"].as_vec().unwrap().iter().enumerate() {
            let policies = &route["policies"];
            if let Some(sender) = policies["extProc"]["requestAttributes"]["sender"].as_str() {
                assert_eq!(sender.trim(), caller, "{path}: route #{i}: extProc's sender");
            }
            for processor in policies["mcpGuardrails"]["processors"].as_vec().into_iter().flatten() {
                let agent = processor["metadata"]["agent"].as_str().unwrap_or_default();
                assert_eq!(agent.trim(), caller, "{path}: route #{i}: mcpGuardrails' agent");
            }
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
