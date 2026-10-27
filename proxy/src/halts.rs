//! The halts in force: the proxy deny lever's state (Brake).
//!
//! The brake puts and lifts halts over the control API (control.rs); agentgateway asks,
//! through ext_authz (authz.rs), whether each call may go ahead. A halt covers the whole
//! fleet or named agents: a call is denied when it's made by an agent in scope, or sent to
//! one. A call whose caller is unknown is denied only by a fleet halt.
//!
//! The brake fails closed. A halt is written to disk before it's acknowledged, so a
//! restart doesn't lift it, and a state file that can't be read halts the fleet until a
//! person or the brake replaces it.

use std::path::{Path, PathBuf};
use std::sync::RwLock;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

/// What a halt covers.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct Scope {
    /// Every call, whoever makes it.
    #[serde(default)]
    pub fleet: bool,
    /// Calls made by, or sent to, these agents: SPIFFE IDs or the names the gateway gives.
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
    /// When this proxy applied it, Unix nanoseconds.
    pub applied_at: u64,
}

/// Every halt in force, and a version that moves on with each change, so the brake can
/// confirm the proxy holds the state it sent.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct State {
    pub version: u64,
    pub halts: Vec<Halt>,
    /// Set when the state on disk couldn't be read: everything is denied.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unreadable: Option<String>,
}

/// The id a call is denied under when the state on disk couldn't be read.
pub const UNREADABLE: &str = "unreadable-state";

pub struct Halts {
    state: RwLock<State>,
    path: Option<PathBuf>,
}

impl Halts {
    /// Halts kept only in memory, for tests.
    pub fn in_memory() -> Self {
        Halts {
            state: RwLock::new(State::default()),
            path: None,
        }
    }

    /// Loads the halts from dir/halts.json. A file that's missing means no halts; one
    /// that can't be read halts everything.
    pub fn load(dir: &Path) -> Self {
        let path = dir.join("halts.json");
        let state = match std::fs::read(&path) {
            Ok(bytes) => match serde_json::from_slice::<State>(&bytes) {
                Ok(state) => state,
                Err(e) => unreadable(&path, &e.to_string()),
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => State::default(),
            Err(e) => unreadable(&path, &e.to_string()),
        };
        Halts {
            state: RwLock::new(state),
            path: Some(path),
        }
    }

    pub fn state(&self) -> State {
        self.state.read().map(|s| s.clone()).unwrap_or_else(|_| poisoned())
    }

    /// Puts a halt in force, or replaces one with the same id, and returns the new state.
    /// It's on disk before this returns.
    pub fn put(&self, id: &str, scope: Scope, issued_at: u64) -> std::io::Result<State> {
        self.change(|state| {
            state.halts.retain(|h| h.id != id);
            state.halts.push(Halt {
                id: id.to_string(),
                scope,
                issued_at,
                applied_at: now(),
            });
        })
    }

    /// Lifts a halt. Lifting one that isn't in force changes nothing but the version.
    pub fn lift(&self, id: &str) -> std::io::Result<State> {
        self.change(|state| {
            state.halts.retain(|h| h.id != id);
            if id == UNREADABLE {
                state.unreadable = None;
            }
        })
    }

    fn change(&self, f: impl FnOnce(&mut State)) -> std::io::Result<State> {
        let mut guard = self
            .state
            .write()
            .map_err(|_| std::io::Error::other("halt state lock poisoned"))?;
        let mut next = guard.clone();
        f(&mut next);
        next.version += 1;
        if let Some(path) = &self.path {
            write_atomically(path, &serde_json::to_vec_pretty(&next)?)?;
        }
        *guard = next.clone();
        Ok(next)
    }

    /// The halt that denies a call from caller to receiver, if any.
    pub fn deciding(&self, caller: Option<&str>, receiver: Option<&str>) -> Option<Halt> {
        let state = self.state.read().map(|s| s.clone()).unwrap_or_else(|_| poisoned());
        if state.unreadable.is_some() {
            return Some(Halt {
                id: UNREADABLE.into(),
                scope: Scope {
                    fleet: true,
                    agents: vec![],
                },
                issued_at: 0,
                applied_at: 0,
            });
        }
        let named = |agent: Option<&str>, halt: &Halt| {
            agent.is_some_and(|a| !a.is_empty() && halt.scope.agents.iter().any(|h| h == a))
        };
        state
            .halts
            .into_iter()
            .find(|h| h.scope.fleet || named(caller, h) || named(receiver, h))
    }
}

fn unreadable(path: &Path, error: &str) -> State {
    tracing::error!(path = %path.display(), error, "can't read the halts in force; denying every call until they're replaced");
    State {
        version: 0,
        halts: vec![],
        unreadable: Some(error.to_string()),
    }
}

fn poisoned() -> State {
    State {
        unreadable: Some("halt state lock poisoned".into()),
        ..Default::default()
    }
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

    fn agents(names: &[&str]) -> Scope {
        Scope {
            fleet: false,
            agents: names.iter().map(|s| s.to_string()).collect(),
        }
    }

    #[test]
    fn denies_calls_by_and_to_agents_in_scope() {
        let halts = Halts::in_memory();
        assert!(halts.deciding(Some("billing"), None).is_none());
        let state = halts.put("h1", agents(&["billing"]), 7).unwrap();
        assert_eq!((state.version, state.halts.len()), (1, 1));
        assert_eq!(halts.deciding(Some("billing"), None).unwrap().id, "h1");
        assert_eq!(halts.deciding(Some("support"), Some("billing")).unwrap().id, "h1");
        assert!(halts.deciding(Some("support"), Some("research")).is_none());
        assert!(
            halts.deciding(None, None).is_none(),
            "an unknown caller only a fleet halt denies"
        );
        assert!(halts.deciding(Some(""), Some("")).is_none());

        halts
            .put(
                "h2",
                Scope {
                    fleet: true,
                    agents: vec![],
                },
                8,
            )
            .unwrap();
        assert!(halts.deciding(None, None).is_some());
        let state = halts.lift("h2").unwrap();
        assert_eq!(state.version, 3);
        assert!(halts.deciding(None, None).is_none());
        assert!(halts.deciding(Some("billing"), None).is_some(), "h1 is still in force");
    }

    #[test]
    fn halts_survive_a_restart() {
        let dir = std::env::temp_dir().join(format!("wake-halts-{}", uuid::Uuid::new_v4()));
        let halts = Halts::load(&dir);
        halts
            .put(
                "h1",
                Scope {
                    fleet: true,
                    agents: vec![],
                },
                1,
            )
            .unwrap();
        let restarted = Halts::load(&dir);
        assert_eq!(restarted.state(), halts.state());
        assert!(restarted.deciding(Some("anyone"), None).is_some());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn state_that_cant_be_read_halts_everything() {
        let dir = std::env::temp_dir().join(format!("wake-halts-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("halts.json"), b"{not json").unwrap();
        let halts = Halts::load(&dir);
        assert_eq!(halts.deciding(None, None).unwrap().id, UNREADABLE);
        assert!(halts.state().unreadable.is_some());
        // Lifting it, once a person has looked, writes a good state again.
        halts.lift(UNREADABLE).unwrap();
        assert!(halts.deciding(None, None).is_none());
        assert!(Halts::load(&dir).state().unreadable.is_none());
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
