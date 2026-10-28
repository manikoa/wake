//! The halts in force: the proxy deny lever's state (Brake).
//!
//! The brake puts and lifts halts over the control API (control.rs); agentgateway asks,
//! through ext_authz (authz.rs), whether each call may go ahead. A halt covers the whole
//! fleet or named agents: a call is denied when it's made by an agent in scope, or sent to
//! one. While any agent is halted, a call whose caller is unknown is denied too, since it
//! could be that agent's. Agents are named through the identity map (agents.rs).
//!
//! The brake fails closed:
//! - A change is in force before it's written, and ext_authz reads the state without ever
//!   waiting on the disk. If the write fails, the change stays in force and the answer
//!   says it wasn't persisted.
//! - A change is acknowledged once it's on disk, so a restart doesn't lift a halt.
//! - The state must be initialized once (`wake-proxy init`), which gives it an epoch. A
//!   proxy that starts without its state, its volume missing or wiped, denies every call
//!   until a person or the brake initializes it again. A new epoch tells the brake the
//!   state started over.
//! - A state file that can't be read denies every call until a person or the brake
//!   replaces it, and is kept aside, not overwritten.
//!
//! The same state holds memory quarantines, which the SDKs' memory adapters fetch
//! (memory.rs): agents whose memory writes are refused, and entries no longer served.
//!
//! Each change is announced (changes()), so a call already under way when a halt lands can
//! be cut: an A2A exchange on an ext_proc stream, or an MCP result on its way back.

use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tokio::sync::watch;

use crate::agents::Agents;

/// What a halt covers.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct Scope {
    /// Every call, whoever makes it.
    #[serde(default)]
    pub fleet: bool,
    /// Calls made by, or sent to, these agents: their names, or identities the map lists.
    #[serde(default)]
    pub agents: Vec<String>,
}

impl Scope {
    pub fn is_empty(&self) -> bool {
        !self.fleet && self.agents.is_empty()
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Halt {
    pub id: String,
    pub scope: Scope,
    /// When the brake issued it, Unix nanoseconds.
    pub issued_at: u64,
    /// When this proxy started enforcing it, Unix nanoseconds: no call in scope is allowed
    /// after it.
    pub applied_at: u64,
}

/// Which memory a quarantine covers. Writes by the agents in `lock` are refused, and an
/// entry is no longer served when its label says it was written by one of `agents`, carries
/// one of `origins`, or came from one of the `writes` (memory_write event ids), when it's
/// one of `entries` ("<store>/<entry>"), or, with `unlabeled`, when no label vouches for it.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Selection {
    #[serde(default)]
    pub lock: Scope,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub agents: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub origins: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub writes: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub entries: Vec<String>,
    #[serde(default)]
    pub unlabeled: bool,
}

impl Selection {
    pub fn is_empty(&self) -> bool {
        self.lock.is_empty()
            && self.agents.is_empty()
            && self.origins.is_empty()
            && self.writes.is_empty()
            && self.entries.is_empty()
            && !self.unlabeled
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Quarantine {
    pub id: String,
    pub selection: Selection,
    pub issued_at: u64,
    pub applied_at: u64,
}

/// What one agent's memory adapter is told: whether its writes are refused, and which
/// entries it mustn't serve.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct MemoryRules {
    pub version: u64,
    /// The quarantine that refuses this agent's writes, if any.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub locked_by: Option<String>,
    /// Set when no entry may be served at all, because the proxy doesn't know what it
    /// quarantined: the id refused reads are recorded under.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hidden_by: Option<String>,
    pub quarantines: Vec<Quarantine>,
}

/// When an agent's memory adapter last fetched its rules, and at which version.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Seen {
    pub version: u64,
    pub at: u64,
}

/// Every halt and quarantine in force, and a version that moves on with each change, so
/// the brake can confirm the proxy holds the state it sent.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct State {
    pub version: u64,
    /// Set when the state was initialized. A new epoch means it started over, so halts put
    /// before it may be gone.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub epoch: String,
    pub halts: Vec<Halt>,
    #[serde(default)]
    pub quarantines: Vec<Quarantine>,
    /// Set when the state on disk couldn't be read: everything is denied.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unreadable: Option<String>,
    /// Set until the state is initialized: everything is denied.
    #[serde(default, skip_serializing_if = "is_false")]
    pub uninitialized: bool,
    /// Set, with why, when this state is in force but couldn't be written to disk: a
    /// restart would lose it. Never on disk.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub not_persisted: Option<String>,
}

fn is_false(b: &bool) -> bool {
    !b
}

/// The id a call is denied under when the state on disk couldn't be read.
pub const UNREADABLE: &str = "unreadable-state";
/// The id a call is denied under until the state is initialized.
pub const UNINITIALIZED: &str = "uninitialized-state";

/// The file the state is kept in, in the state directory.
pub const FILE: &str = "halts.json";

/// A change that's in force but couldn't be written to disk, so a restart would lose it.
#[derive(Debug)]
pub struct NotPersisted {
    /// The state now in force, with `not_persisted` set.
    pub state: Box<State>,
    pub error: std::io::Error,
}

/// The state in force after a change, or why it isn't on disk.
pub type Changed = Result<State, NotPersisted>;

pub struct Halts {
    // Swapped whole on each change. Readers only clone the Arc, so they never wait on a
    // change being written.
    current: RwLock<Arc<State>>,
    // One change at a time, held across the disk write.
    disk: Mutex<Disk>,
    agents: Arc<Agents>,
    // Kept in memory only: after a restart, adapters fetch again within seconds.
    seen: Mutex<Notes>,
    // The version in force, sent on each change.
    changed: watch::Sender<u64>,
}

/// The most agents whose fetches are noted. Agents name themselves, so past it the oldest
/// note goes, and the brake can't confirm that agent's adapter until it fetches again.
pub const SEEN_LIMIT: usize = 10_000;

#[derive(Default)]
struct Notes {
    by_agent: HashMap<String, Seen>,
    order: VecDeque<String>,
}

struct Disk {
    path: Option<PathBuf>,
    // The file on disk couldn't be read; it's moved aside before it's first replaced.
    unreadable: bool,
}

impl Halts {
    /// Halts kept only in memory, for tests: initialized, with nothing in force.
    pub fn in_memory() -> Self {
        Halts::new(
            State {
                epoch: new_epoch(),
                ..Default::default()
            },
            None,
            false,
        )
    }

    /// Loads the halts from dir/halts.json. A file that's missing means the state was never
    /// initialized, or was lost; one that can't be read is kept and halts everything.
    pub fn load(dir: &Path) -> Self {
        let path = dir.join(FILE);
        let (state, unreadable) = match std::fs::read(&path) {
            Ok(bytes) => match serde_json::from_slice::<State>(&bytes) {
                Ok(state) => (state, false),
                Err(e) => (unreadable_state(&path, &e.to_string()), true),
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                tracing::error!(
                    path = %path.display(),
                    "no halts state; denying every call until it's initialized (wake-proxy init)"
                );
                (
                    State {
                        uninitialized: true,
                        ..Default::default()
                    },
                    false,
                )
            }
            Err(e) => (unreadable_state(&path, &e.to_string()), true),
        };
        Halts::new(state, Some(path), unreadable)
    }

    fn new(state: State, path: Option<PathBuf>, unreadable: bool) -> Self {
        Halts {
            changed: watch::Sender::new(state.version),
            current: RwLock::new(Arc::new(state)),
            disk: Mutex::new(Disk { path, unreadable }),
            agents: Arc::new(Agents::none()),
            seen: Mutex::new(Notes::default()),
        }
    }

    /// Names callers and halted agents through this identity map.
    pub fn with_agents(mut self, agents: Arc<Agents>) -> Self {
        self.agents = agents;
        self
    }

    /// The identity map callers and halted agents are named through.
    pub fn agents(&self) -> &Arc<Agents> {
        &self.agents
    }

    /// Writes a new, empty state with a new epoch to dir/halts.json, which mustn't exist.
    pub fn init(dir: &Path) -> std::io::Result<State> {
        let path = dir.join(FILE);
        if path.try_exists()? {
            return Err(std::io::Error::new(
                std::io::ErrorKind::AlreadyExists,
                format!("{} already exists", path.display()),
            ));
        }
        let state = State {
            epoch: new_epoch(),
            ..Default::default()
        };
        write_atomically(&path, &serde_json::to_vec_pretty(&state)?)?;
        Ok(state)
    }

    fn current(&self) -> Arc<State> {
        self.current.read().unwrap_or_else(|p| p.into_inner()).clone()
    }

    fn swap(&self, state: State) {
        *self.current.write().unwrap_or_else(|p| p.into_inner()) = Arc::new(state);
    }

    pub fn state(&self) -> State {
        (*self.current()).clone()
    }

    /// Says when the state changes: the version in force, from the next change on.
    pub fn changes(&self) -> watch::Receiver<u64> {
        self.changed.subscribe()
    }

    /// Puts a halt in force, or replaces one with the same id, and returns the new state.
    pub fn put(&self, id: &str, scope: Scope, issued_at: u64) -> Changed {
        self.change(|state| {
            state.halts.retain(|h| h.id != id);
            state.halts.push(Halt {
                id: id.to_string(),
                scope,
                issued_at,
                applied_at: STAMP,
            });
        })
    }

    /// Lifts a halt. Lifting one that isn't in force changes nothing but the version.
    /// Lifting UNREADABLE or UNINITIALIZED starts the state over, with a new epoch.
    pub fn lift(&self, id: &str) -> Changed {
        self.change(|state| {
            state.halts.retain(|h| h.id != id);
            if (id == UNREADABLE && state.unreadable.take().is_some())
                || (id == UNINITIALIZED && std::mem::take(&mut state.uninitialized))
            {
                state.epoch = new_epoch();
            }
        })
    }

    /// Puts a memory quarantine in force, or replaces one with the same id.
    pub fn quarantine(&self, id: &str, selection: Selection, issued_at: u64) -> Changed {
        self.change(|state| {
            state.quarantines.retain(|q| q.id != id);
            state.quarantines.push(Quarantine {
                id: id.to_string(),
                selection,
                issued_at,
                applied_at: STAMP,
            });
        })
    }

    /// Lifts a memory quarantine. Lifting one that isn't in force changes nothing but the
    /// version.
    pub fn release(&self, id: &str) -> Changed {
        self.change(|state| state.quarantines.retain(|q| q.id != id))
    }

    /// The rules for one agent's memory adapter, noting that the agent has them now. A
    /// state that can't be read, or was never initialized, refuses every write and serves
    /// no entry: whatever it quarantined is unknown, so any entry might be.
    pub fn memory_rules(&self, agent: &str) -> MemoryRules {
        let state = self.current();
        let agent = self.agents.named(agent);
        let named = |scope: &Scope| {
            scope.fleet || (!agent.is_empty() && scope.agents.iter().any(|a| self.agents.named(a) == agent))
        };
        let unknown = if state.unreadable.is_some() {
            Some(UNREADABLE.to_string())
        } else if state.uninitialized {
            Some(UNINITIALIZED.to_string())
        } else {
            None
        };
        let locked_by = unknown.clone().or_else(|| {
            state
                .quarantines
                .iter()
                .find(|q| named(&q.selection.lock))
                .map(|q| q.id.clone())
        });
        if !agent.is_empty() {
            let mut notes = self.seen.lock().unwrap_or_else(|p| p.into_inner());
            let seen = Seen {
                version: state.version,
                at: now(),
            };
            if notes.by_agent.insert(agent.to_string(), seen).is_none() {
                notes.order.push_back(agent.to_string());
            }
            while notes.order.len() > SEEN_LIMIT {
                if let Some(oldest) = notes.order.pop_front() {
                    notes.by_agent.remove(&oldest);
                }
            }
        }
        MemoryRules {
            version: state.version,
            locked_by,
            hidden_by: unknown,
            quarantines: state.quarantines.clone(),
        }
    }

    /// Each agent whose memory adapter has fetched its rules, with the version it got.
    pub fn seen(&self) -> HashMap<String, Seen> {
        self.seen.lock().unwrap_or_else(|p| p.into_inner()).by_agent.clone()
    }

    fn change(&self, f: impl FnOnce(&mut State)) -> Changed {
        let mut disk = self.disk.lock().unwrap_or_else(|p| p.into_inner());
        let mut next = (*self.current()).clone();
        f(&mut next);
        next.version += 1;
        next.not_persisted = None;
        if next.epoch.is_empty() && !next.uninitialized && next.unreadable.is_none() {
            // A state written before epochs existed.
            next.epoch = new_epoch();
        }
        // In force from here on, before the disk write.
        self.swap(next.clone());
        self.changed.send_replace(next.version);
        // Stamped after the swap, so no call in scope is allowed after applied_at.
        let at = now();
        for h in next.halts.iter_mut().filter(|h| h.applied_at == STAMP) {
            h.applied_at = at;
        }
        for q in next.quarantines.iter_mut().filter(|q| q.applied_at == STAMP) {
            q.applied_at = at;
        }
        self.swap(next.clone());
        match disk.write(&next) {
            Ok(()) => Ok(next),
            Err(error) => {
                tracing::error!(error = %error, version = next.version, "couldn't store the halts; they're in force until a restart");
                next.not_persisted = Some(error.to_string());
                self.swap(next.clone());
                Err(NotPersisted {
                    state: Box::new(next),
                    error,
                })
            }
        }
    }

    /// Every halt that denies a call from caller, the identity the gateway authenticated, to
    /// receiver, the agent the route reaches: the brake's halts in the order they were put,
    /// then UNREADABLE or UNINITIALIZED. Empty when the call may go ahead.
    pub fn deciding(&self, caller: Option<&str>, receiver: Option<&str>) -> Vec<Halt> {
        let state = self.current();
        let caller = self.agents.caller(caller);
        let receiver = receiver.filter(|r| !r.is_empty()).map(|r| self.agents.named(r));
        let names = |agent: Option<&str>, halt: &Halt| {
            agent.is_some_and(|a| halt.scope.agents.iter().any(|h| self.agents.named(h) == a))
        };
        let mut out: Vec<Halt> = state
            .halts
            .iter()
            .filter(|h| {
                h.scope.fleet
                    || names(caller, h)
                    || names(receiver, h)
                    // A caller nobody can name could be the halted agent.
                    || (caller.is_none() && !h.scope.agents.is_empty())
            })
            .cloned()
            .collect();
        for (id, failed) in [
            (UNREADABLE, state.unreadable.is_some()),
            (UNINITIALIZED, state.uninitialized),
        ] {
            if failed {
                out.push(Halt {
                    id: id.into(),
                    scope: Scope {
                        fleet: true,
                        agents: vec![],
                    },
                    issued_at: 0,
                    applied_at: 0,
                });
            }
        }
        out
    }
}

// A halt or quarantine's applied_at until the change puts it in force.
const STAMP: u64 = 0;

impl Disk {
    fn write(&mut self, state: &State) -> std::io::Result<()> {
        let Some(path) = &self.path else { return Ok(()) };
        if self.unreadable {
            // Kept as evidence of what went wrong, rather than written over.
            let aside = path.with_extension(format!("unreadable-{}.json", now()));
            match std::fs::rename(path, &aside) {
                Ok(()) => tracing::warn!(path = %aside.display(), "kept the unreadable halts state aside"),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(e),
            }
            self.unreadable = false;
        }
        write_atomically(path, &serde_json::to_vec_pretty(state)?)
    }
}

fn unreadable_state(path: &Path, error: &str) -> State {
    tracing::error!(path = %path.display(), error, "can't read the halts in force; denying every call until they're replaced");
    State {
        unreadable: Some(error.to_string()),
        ..Default::default()
    }
}

fn new_epoch() -> String {
    uuid::Uuid::new_v4().to_string()
}

/// Writes the file whole or not at all: a temporary file, synced, renamed over the old one,
/// and the directory synced.
fn write_atomically(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    let dir = path.parent().unwrap_or(Path::new("."));
    std::fs::create_dir_all(dir)?;
    let tmp = path.with_extension("json.tmp");
    {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
    }
    std::fs::rename(&tmp, path)?;
    std::fs::File::open(dir)?.sync_all()
}

pub fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_change_is_announced_once_in_force() {
        let halts = Halts::in_memory();
        let mut changes = halts.changes();
        assert!(!changes.has_changed().unwrap(), "nothing has changed yet");
        halts.put("h1", fleet(), 1).unwrap();
        assert!(changes.has_changed().unwrap());
        assert_eq!(*changes.borrow_and_update(), 1);
        assert!(
            !halts.deciding(Some("anyone"), None).is_empty(),
            "in force by the time it's announced"
        );
        halts.lift("h1").unwrap();
        assert_eq!(*changes.borrow_and_update(), 2);
    }

    fn agents(names: &[&str]) -> Scope {
        Scope {
            fleet: false,
            agents: names.iter().map(|s| s.to_string()).collect(),
        }
    }

    fn fleet() -> Scope {
        Scope {
            fleet: true,
            agents: vec![],
        }
    }

    fn ids(halts: Vec<Halt>) -> Vec<String> {
        halts.into_iter().map(|h| h.id).collect()
    }

    fn temp_dir() -> PathBuf {
        std::env::temp_dir().join(format!("wake-halts-{}", uuid::Uuid::new_v4()))
    }

    #[test]
    fn denies_calls_by_and_to_agents_in_scope() {
        let halts = Halts::in_memory();
        assert!(halts.deciding(Some("billing"), None).is_empty());
        assert!(halts.deciding(None, None).is_empty(), "nothing is halted");
        let state = halts.put("h1", agents(&["billing"]), 7).unwrap();
        assert_eq!((state.version, state.halts.len()), (1, 1));
        assert_eq!(ids(halts.deciding(Some("billing"), None)), ["h1"]);
        assert_eq!(ids(halts.deciding(Some("support"), Some("billing"))), ["h1"]);
        assert!(halts.deciding(Some("support"), Some("research")).is_empty());

        halts.put("h2", fleet(), 8).unwrap();
        assert_eq!(ids(halts.deciding(Some("support"), None)), ["h2"]);
        let state = halts.lift("h2").unwrap();
        assert_eq!(state.version, 3);
        assert!(halts.deciding(Some("support"), None).is_empty());
        assert_eq!(
            ids(halts.deciding(Some("billing"), None)),
            ["h1"],
            "h1 is still in force"
        );
    }

    #[test]
    fn an_agent_halt_denies_callers_nobody_can_name() {
        let halts = Halts::in_memory();
        halts.put("h1", agents(&["billing"]), 7).unwrap();
        // Billing could leave out its identity; while it's halted, nobody may.
        assert_eq!(ids(halts.deciding(None, None)), ["h1"]);
        assert_eq!(ids(halts.deciding(Some(""), Some("research"))), ["h1"]);
        halts.lift("h1").unwrap();
        assert!(halts.deciding(None, None).is_empty());
    }

    #[test]
    fn names_every_halt_that_covers_a_call() {
        let halts = Halts::in_memory();
        halts.put("fleet-1", fleet(), 1).unwrap();
        halts.put("billing", agents(&["billing"]), 2).unwrap();
        halts.put("fleet-2", fleet(), 3).unwrap();
        assert_eq!(
            ids(halts.deciding(Some("billing"), None)),
            ["fleet-1", "billing", "fleet-2"]
        );
        assert_eq!(ids(halts.deciding(Some("support"), None)), ["fleet-1", "fleet-2"]);
    }

    #[test]
    fn the_identity_map_names_callers_and_halted_agents() {
        let map =
            br#"{"billing": ["spiffe://wake.local/agent/billing"], "support": ["spiffe://wake.local/agent/support"]}"#;
        let halts = Halts::in_memory().with_agents(Arc::new(Agents::from_json(map).unwrap()));
        // The brake halts by the short name; the gateway sends the SPIFFE ID.
        halts.put("h1", agents(&["billing"]), 1).unwrap();
        assert_eq!(
            ids(halts.deciding(Some("spiffe://wake.local/agent/billing"), None)),
            ["h1"]
        );
        assert!(
            halts
                .deciding(Some("spiffe://wake.local/agent/support"), Some("research"))
                .is_empty()
        );
        // An identity the map doesn't list is a caller nobody can name.
        assert_eq!(ids(halts.deciding(Some("spiffe://wake.local/agent/new"), None)), ["h1"]);
        // A halt may name the identity too.
        halts.lift("h1").unwrap();
        halts
            .put("h2", agents(&["spiffe://wake.local/agent/support"]), 2)
            .unwrap();
        assert_eq!(
            ids(halts.deciding(Some("spiffe://wake.local/agent/support"), None)),
            ["h2"]
        );
        assert_eq!(
            ids(halts.deciding(Some("spiffe://wake.local/agent/billing"), Some("support"))),
            ["h2"]
        );
    }

    #[test]
    fn applied_at_is_when_the_halt_went_into_force() {
        let halts = Halts::in_memory();
        let before = now();
        let state = halts.put("h1", fleet(), 7).unwrap();
        let after = now();
        let applied = state.halts[0].applied_at;
        assert!(before <= applied && applied <= after);
        assert_eq!(halts.state().halts[0].applied_at, applied);
        let state = halts
            .quarantine(
                "q1",
                Selection {
                    unlabeled: true,
                    ..Default::default()
                },
                8,
            )
            .unwrap();
        assert!(state.quarantines[0].applied_at >= applied);
    }

    #[test]
    fn quarantines_tell_each_agents_adapter_what_to_refuse() {
        let halts = Halts::in_memory();
        let selection = Selection {
            lock: agents(&["billing"]),
            origins: vec!["tool:fetch_page".into()],
            ..Default::default()
        };
        let state = halts.quarantine("q1", selection, 5).unwrap();
        assert_eq!((state.version, state.quarantines.len()), (1, 1));

        let billing = halts.memory_rules("billing");
        assert_eq!(billing.locked_by.as_deref(), Some("q1"));
        assert_eq!(billing.quarantines[0].selection.origins, vec!["tool:fetch_page"]);
        let support = halts.memory_rules("support");
        assert_eq!(support.locked_by, None, "only billing's writes are refused");
        assert_eq!(
            support.quarantines.len(),
            1,
            "every agent hides the quarantined entries"
        );
        assert_eq!(halts.memory_rules("").locked_by, None);

        let seen = halts.seen();
        assert_eq!(seen["billing"].version, 1);
        assert!(seen.contains_key("support") && !seen.contains_key(""));

        halts
            .quarantine(
                "q2",
                Selection {
                    lock: fleet(),
                    ..Default::default()
                },
                6,
            )
            .unwrap();
        assert_eq!(halts.memory_rules("support").locked_by.as_deref(), Some("q2"));
        halts.release("q1").unwrap();
        let state = halts.release("q2").unwrap();
        assert_eq!((state.version, state.quarantines.len()), (4, 0));
        assert_eq!(halts.memory_rules("billing").locked_by, None);
    }

    #[test]
    fn the_notes_of_who_fetched_are_bounded() {
        let halts = Halts::in_memory();
        for i in 0..SEEN_LIMIT + 10 {
            halts.memory_rules(&format!("agent-{i}"));
        }
        let seen = halts.seen();
        assert_eq!(seen.len(), SEEN_LIMIT);
        assert!(!seen.contains_key("agent-0"), "the oldest note went");
        assert!(seen.contains_key(&format!("agent-{}", SEEN_LIMIT + 9)));
    }

    #[test]
    fn halts_survive_a_restart() {
        let dir = temp_dir();
        let epoch = Halts::init(&dir).unwrap().epoch;
        let halts = Halts::load(&dir);
        assert_eq!(halts.state().epoch, epoch);
        halts.put("h1", fleet(), 1).unwrap();
        let restarted = Halts::load(&dir);
        assert_eq!(restarted.state(), halts.state());
        assert_eq!(ids(restarted.deciding(Some("anyone"), None)), ["h1"]);
        assert_eq!(restarted.state().epoch, epoch, "the same state, carried on");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_state_never_initialized_or_lost_halts_everything() {
        let dir = temp_dir();
        let halts = Halts::load(&dir);
        assert!(halts.state().uninitialized);
        assert_eq!(ids(halts.deciding(Some("anyone"), None)), [UNINITIALIZED]);
        let rules = halts.memory_rules("billing");
        assert_eq!(rules.locked_by.as_deref(), Some(UNINITIALIZED));
        assert_eq!(
            rules.hidden_by.as_deref(),
            Some(UNINITIALIZED),
            "no entry is served while what was quarantined is unknown"
        );
        // A halt put meanwhile is kept, and the state stays uninitialized across a restart.
        halts.put("h1", agents(&["billing"]), 1).unwrap();
        let restarted = Halts::load(&dir);
        assert_eq!(ids(restarted.deciding(Some("billing"), None)), ["h1", UNINITIALIZED]);
        // The brake, or a person, initializes it: a new epoch, and h1 still in force.
        let state = restarted.lift(UNINITIALIZED).unwrap();
        assert!(!state.uninitialized && !state.epoch.is_empty());
        assert!(restarted.deciding(Some("support"), None).is_empty());
        assert_eq!(restarted.memory_rules("billing").hidden_by, None);
        assert!(!Halts::load(&dir).state().uninitialized);
        // Init refuses to write over a state.
        let err = Halts::init(&dir).unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::AlreadyExists);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn state_that_cant_be_read_halts_everything_and_is_kept_aside() {
        let dir = temp_dir();
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(FILE), b"{not json").unwrap();
        let halts = Halts::load(&dir);
        assert_eq!(ids(halts.deciding(None, None)), [UNREADABLE]);
        assert!(halts.state().unreadable.is_some());
        let rules = halts.memory_rules("billing");
        assert_eq!(rules.locked_by.as_deref(), Some(UNREADABLE), "memory writes too");
        assert_eq!(
            rules.hidden_by.as_deref(),
            Some(UNREADABLE),
            "and no entry is served, since what was quarantined is unknown"
        );
        // A restart still can't read it.
        assert!(Halts::load(&dir).state().unreadable.is_some());
        // A halt put meanwhile doesn't lift it, and doesn't write over what couldn't be read.
        halts.put("h1", fleet(), 1).unwrap();
        assert!(Halts::load(&dir).state().unreadable.is_some());
        let kept: Vec<_> = std::fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .filter(|n| n.contains("unreadable"))
            .collect();
        assert_eq!(kept.len(), 1, "{kept:?}");
        assert_eq!(std::fs::read(dir.join(&kept[0])).unwrap(), b"{not json");
        // Lifting it, once a person has looked, starts the state over.
        let state = halts.lift(UNREADABLE).unwrap();
        assert!(state.unreadable.is_none() && !state.epoch.is_empty());
        assert_eq!(ids(halts.deciding(None, None)), ["h1"]);
        assert!(Halts::load(&dir).state().unreadable.is_none());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_halt_that_cant_be_written_is_still_enforced() {
        let dir = temp_dir();
        Halts::init(&dir).unwrap();
        let halts = Halts::load(&dir);
        // The disk goes away: the directory is replaced by a file, so writes fail.
        std::fs::remove_dir_all(&dir).unwrap();
        std::fs::write(&dir, b"").unwrap();
        let err = halts.put("h1", fleet(), 1).unwrap_err();
        assert!(err.state.not_persisted.is_some());
        assert_eq!(ids(halts.deciding(Some("billing"), None)), ["h1"], "in force anyway");
        assert!(
            halts.state().not_persisted.is_some(),
            "and the brake can see it isn't on disk"
        );
        // Once the disk is back, the next change writes everything in force.
        std::fs::remove_file(&dir).unwrap();
        let state = halts.put("h2", agents(&["billing"]), 2).unwrap();
        assert!(state.not_persisted.is_none());
        assert_eq!(ids(Halts::load(&dir).deciding(Some("billing"), None)), ["h1", "h2"]);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn readers_never_wait_on_a_change_being_written() {
        let halts = Arc::new(Halts::in_memory());
        // Hold the change lock, as a write stuck on the disk would.
        let stuck = halts.disk.lock().unwrap();
        let reader = {
            let halts = halts.clone();
            std::thread::spawn(move || halts.deciding(Some("billing"), None).len())
        };
        assert_eq!(reader.join().unwrap(), 0);
        drop(stuck);
    }

    #[test]
    fn a_state_from_before_epochs_is_carried_on() {
        let dir = temp_dir();
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join(FILE), br#"{"version": 4, "halts": []}"#).unwrap();
        let halts = Halts::load(&dir);
        assert!(halts.deciding(Some("anyone"), None).is_empty());
        let state = halts.put("h1", fleet(), 1).unwrap();
        assert_eq!(state.version, 5);
        assert!(!state.epoch.is_empty());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
