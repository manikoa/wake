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
//! (memory.rs): agents whose memory writes are refused, and entries no longer served. And
//! run charters (charters.rs): what a run must never do, its budget and when it expires.
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
use crate::charters::{self, Charter, Resource};
use crate::token::Token;

/// What a halt covers. A field it doesn't know is refused, not dropped: a scope misspelled
/// would be acknowledged and cover less than the brake asked.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub struct Scope {
    /// Every call, whoever makes it.
    #[serde(default)]
    pub fleet: bool,
    /// Calls made by, or sent to, these agents: their names, or identities the map lists.
    /// A call is made by an agent too when the agent is in the delegation chain of the token
    /// it came with: halting an agent halts the sub-agents acting under it.
    #[serde(default)]
    pub agents: Vec<String>,
    /// Calls made with these tokens, by their id (a JWT's jti, or its uti without one,
    /// token.rs), whoever presents them: one sub-agent's credential, stopped without its
    /// parent's.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub tokens: Vec<String>,
}

impl Scope {
    pub fn is_empty(&self) -> bool {
        !self.fleet && self.agents.is_empty() && self.tokens.is_empty()
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
    /// The halts in force that name agents, for an SDK to stop the agent, or one instance of
    /// it, in its own process: what each covers, never which tokens. A state that can't be
    /// read, or isn't initialized, is a halt of the fleet, as the deny makes it. Always
    /// sent, so an SDK can tell none from a proxy that doesn't say.
    #[serde(default)]
    pub halts: Vec<HaltRule>,
}

/// A halt as an agent's SDK is told of it.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct HaltRule {
    pub id: String,
    #[serde(default, skip_serializing_if = "is_false")]
    pub fleet: bool,
    /// The agents and sub-agents it names, as it names them, and the name of each agent it
    /// names by an identity the agents file lists.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub agents: Vec<String>,
}

/// What an agent's SDKs have of its memory rules, as the brake reads it: the version, and
/// when the process that has it fetched it.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct Seen {
    pub version: u64,
    pub at: u64,
    /// Set when every process still holding the agent back said, with its last fetch, that
    /// its integration refuses the calls of halted instances (`stops=1`). The brake needs
    /// it before it counts a stop in the agent's own process as confirmed.
    #[serde(default, skip_serializing_if = "is_false")]
    pub stops: bool,
}

/// What an SDK says of the process fetching its agent's rules.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Fetcher {
    /// How long after this fetch the process still holds its agent back, in nanoseconds.
    pub hold: u64,
    /// Whether its integration refuses the calls of halted instances (`stops=1`).
    pub stops: bool,
}

impl Default for Fetcher {
    /// An SDK that says nothing of its process: one that acts on rules for 30 seconds.
    fn default() -> Self {
        Fetcher {
            hold: PROCESS_LIVE,
            stops: false,
        }
    }
}

impl Fetcher {
    /// A process whose SDK acts on the rules it has for ttl seconds after it fetched them
    /// (its stale-after setting), if it says so: held back 10 seconds past that, at least
    /// PROCESS_LIVE and at most PROCESS_LIVE_MAX. A ttl that can't be read could be
    /// anything, so it's held back the longest.
    pub fn new(ttl: Option<&str>, stops: bool) -> Self {
        let hold = match ttl.map(str::parse::<f64>) {
            None => PROCESS_LIVE,
            Some(Ok(secs)) if secs.is_finite() && secs >= 0.0 => ((secs * 1e9) as u64)
                .saturating_add(PAST_TTL)
                .clamp(PROCESS_LIVE, PROCESS_LIVE_MAX),
            Some(_) => PROCESS_LIVE_MAX,
        };
        Fetcher { hold, stops }
    }
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
    /// The run charters in force, in the order they were put.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub charters: Vec<Charter>,
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
    // The callers the deny lever has seen, kept in memory only, like seen.
    callers: Mutex<Callers>,
    // Each charter's never, compiled, and what its budget has used.
    charters: charters::Runtime,
    // Where what the budgets have used is written, apart from the state (save_counts).
    counts: Mutex<CountsDisk>,
    // The version in force, sent on each change.
    changed: watch::Sender<u64>,
}

/// The file what each charter's budget has used is kept in, in the state directory. Apart
/// from the state, so counting a call never moves the version the brake confirms.
pub const COUNTS_FILE: &str = "charter-counts.json";

/// How often what the budgets have used is written, while it changes: a restart counts
/// again at most the calls of this long.
pub const COUNTS_EVERY: std::time::Duration = std::time::Duration::from_secs(1);

/// What each charter's budget has used, limit by limit, as written to COUNTS_FILE.
#[derive(Debug, Default, PartialEq, Serialize, Deserialize)]
struct Counts {
    /// The state's epoch: counts from a state that started over are another state's.
    epoch: String,
    used: HashMap<String, Vec<u64>>,
}

struct CountsDisk {
    path: Option<PathBuf>,
    // The charters' changes() as last written.
    written: u64,
}

/// The most fetches noted, one for each agent and process that fetched. Agents name
/// themselves, so past it the oldest note goes. One whose process still holds its agent back
/// is held back, until the process fetches again or its hold ends.
pub const SEEN_LIMIT: usize = 10_000;
/// The most processes noted for one agent, well above the workers one agent runs. Past it
/// the one whose hold ends first goes, held back like any other.
pub const PROCESSES_LIMIT: usize = 256;
/// The most notes held back at once, across agents. Past it, a note whose process still
/// holds its agent back goes into a floor that holds every agent back to its version until
/// its hold ends, since which agent's it was isn't kept: the brake confirms nothing newer in
/// an agent's own process until then, rather than too soon.
pub const HELD_LIMIT: usize = 10_000;
/// The least time after its last fetch a process still holds its agent back, in
/// nanoseconds. An SDK fetches every 2 seconds and refuses every call once its rules are
/// stale, 30 seconds old unless it's set otherwise, so a process silent this long has
/// stopped, or refuses everything. One whose SDK says it acts on its rules for longer (its
/// `ttl`) holds its agent back for 10 seconds past that, up to PROCESS_LIVE_MAX.
pub const PROCESS_LIVE: u64 = 60_000_000_000;
/// The longest a process holds its agent back after its last fetch, in nanoseconds.
pub const PROCESS_LIVE_MAX: u64 = 600_000_000_000;
/// How long past its SDK's ttl a process still holds its agent back, in nanoseconds: room
/// for the time between the proxy answering a fetch and the SDK taking the rules up.
const PAST_TTL: u64 = 10_000_000_000;

/// One process's last fetch: the version it got and when, how long it holds its agent back
/// from then, and whether its integration stops halted instances.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Note {
    version: u64,
    at: u64,
    hold: u64,
    stops: bool,
}

impl Note {
    fn holds(&self, now: u64) -> bool {
        now.saturating_sub(self.at) < self.hold
    }

    fn until(&self) -> u64 {
        self.at.saturating_add(self.hold)
    }

    /// The note this and other make as one: the version of the one further behind, until
    /// the later of their holds ends, stopping halted instances only if both do.
    fn lower(self, other: Note) -> Note {
        let behind = if (other.version, other.at) < (self.version, self.at) {
            other
        } else {
            self
        };
        let until = self.until().max(other.until());
        Note {
            hold: until - behind.at,
            stops: self.stops && other.stops,
            ..behind
        }
    }
}

/// Who has fetched the memory rules: each agent's processes, by the name each SDK gives its
/// own (an SDK that names none is the process with no name).
#[derive(Default)]
struct Notes {
    by_agent: HashMap<String, Fetches>,
    // Each agent and process with a note in by_process, oldest first.
    order: VecDeque<(String, String)>,
    // How many notes are held back, across agents.
    held: usize,
    // The notes past HELD_LIMIT that still held their agents back, as one: it holds every
    // agent back while it holds.
    floor: Option<Note>,
}

/// One agent's processes and the last fetch of each.
#[derive(Default)]
struct Fetches {
    by_process: HashMap<String, Note>,
    // The notes that went from by_process, to keep within the bounds, while their processes
    // still held the agent back, by process. Each goes when its process fetches again or its
    // hold ends, so a note going never makes the agent look further along than it is.
    held: HashMap<String, Note>,
}

impl Notes {
    fn note(&mut self, agent: &str, process: &str, note: Note) {
        let mut went = vec![];
        let Notes {
            by_agent, order, held, ..
        } = self;
        let fetches = by_agent.entry(agent.to_string()).or_default();
        // Fetching again, the process holds its agent back by this fetch alone.
        if fetches.held.remove(process).is_some() {
            *held -= 1;
        }
        if fetches.by_process.insert(process.to_string(), note).is_none() {
            order.push_back((agent.to_string(), process.to_string()));
            if fetches.by_process.len() > PROCESSES_LIMIT
                && let Some(first) = fetches
                    .by_process
                    .iter()
                    .min_by_key(|(_, n)| n.until())
                    .map(|(p, _)| p.clone())
                && let Some(n) = fetches.by_process.remove(&first)
            {
                order.retain(|(a, p)| !(a == agent && *p == first));
                went.push((agent.to_string(), first, n));
            }
        }
        while order.len() > SEEN_LIMIT {
            let Some((agent, process)) = order.pop_front() else {
                break;
            };
            if let Some(n) = by_agent.get_mut(&agent).and_then(|f| f.by_process.remove(&process)) {
                went.push((agent, process, n));
            }
        }
        for (agent, process, n) in went {
            self.hold_back(&agent, &process, n, note.at);
        }
    }

    /// Holds back a note that went to keep within the bounds, while its process still holds
    /// its agent back. An agent left with no note goes.
    fn hold_back(&mut self, agent: &str, process: &str, note: Note, now: u64) {
        if note.holds(now) {
            if self.held >= HELD_LIMIT {
                self.prune(now);
            }
            if self.held < HELD_LIMIT {
                let fetches = self.by_agent.entry(agent.to_string()).or_default();
                if fetches.held.insert(process.to_string(), note).is_none() {
                    self.held += 1;
                }
            } else {
                self.floor = Some(match self.floor.filter(|f| f.holds(now)) {
                    Some(floor) => floor.lower(note),
                    None => note,
                });
            }
        }
        if self
            .by_agent
            .get(agent)
            .is_some_and(|f| f.by_process.is_empty() && f.held.is_empty())
        {
            self.by_agent.remove(agent);
        }
    }

    /// Lets go of the held-back notes, and the floor, whose holds have ended, and of agents
    /// left with no note.
    fn prune(&mut self, now: u64) {
        let mut held = 0;
        self.by_agent.retain(|_, f| {
            f.held.retain(|_, n| n.holds(now));
            held += f.held.len();
            !f.by_process.is_empty() || !f.held.is_empty()
        });
        self.held = held;
        self.floor = self.floor.filter(|f| f.holds(now));
    }

    /// For each agent, the version its SDKs have (Fetches::have).
    fn have(&mut self, now: u64) -> HashMap<String, Seen> {
        self.prune(now);
        let floor = self.floor;
        self.by_agent
            .iter()
            .filter_map(|(agent, f)| f.have(now, floor).map(|seen| (agent.clone(), seen)))
            .collect()
    }
}

impl Fetches {
    /// What the agent's SDKs have: the fetch furthest behind among its processes still
    /// holding it back, those that fetched within their hold, held back or not, and the
    /// floor while it holds, so that one process having a halt doesn't confirm it while
    /// another running the agent hasn't fetched it yet. It stops halted instances when each
    /// of those does. When none still holds it back, its latest fetch.
    fn have(&self, now: u64, floor: Option<Note>) -> Option<Seen> {
        let holding = || {
            self.by_process
                .values()
                .chain(self.held.values())
                .chain(floor.as_ref())
                .filter(move |n| n.holds(now))
        };
        let seen = |n: &Note, stops: bool| Seen {
            version: n.version,
            at: n.at,
            stops,
        };
        match holding().min_by_key(|n| (n.version, n.at)) {
            Some(behind) => Some(seen(behind, holding().all(|n| n.stops))),
            None => self.by_process.values().max_by_key(|n| n.at).map(|n| seen(n, n.stops)),
        }
    }
}

/// One identity the deny lever has seen calling, for the brake: under which agent the
/// identity map names it, whom its latest token acted for and through which agents, the
/// latest ids of the tokens it presented, and when. With it the brake can tell whether a
/// sub-agent calls under credentials of its own, which a halt can stop alone, or only ever
/// as its parent.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Caller {
    /// The agent the identity map names it as, if it lists it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent: Option<String>,
    /// Whom its latest token acted for, the token's `sub`; none without a token.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub subject: Option<String>,
    /// The delegation chain of its latest token, the agent acting now first; empty without one.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub actors: Vec<String>,
    /// Whether that chain went on past the actors kept (token.rs).
    #[serde(default, skip_serializing_if = "is_false")]
    pub actors_cut: bool,
    /// Set when the agents file doesn't list it and its latest token names another agent
    /// as the one acting: it can't be placed under the agent it acts for, so every agent
    /// halt denies it as a caller nobody can place. Usually an mTLS caller whose identity
    /// provider doesn't name it by its SPIFFE ID in the token's `act`.
    #[serde(default, skip_serializing_if = "is_false")]
    pub token_names_another: bool,
    /// The ids of the latest tokens it presented, newest last, at most CALLER_TOKENS.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub tokens: Vec<String>,
    pub first_seen: u64,
    pub last_seen: u64,
    pub calls: u64,
}

/// The most identities the deny lever notes. Past it the one seen longest ago goes.
pub const CALLERS_LIMIT: usize = 10_000;
/// The most token ids kept for one identity.
pub const CALLER_TOKENS: usize = 8;

#[derive(Default)]
struct Callers {
    by_identity: HashMap<String, Caller>,
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
        let halts = Halts::new(state, Some(path), unreadable);
        halts.take_up_counts();
        halts
    }

    fn new(state: State, path: Option<PathBuf>, unreadable: bool) -> Self {
        let counts = path.as_ref().map(|p| p.with_file_name(COUNTS_FILE));
        Halts {
            changed: watch::Sender::new(state.version),
            current: RwLock::new(Arc::new(state)),
            disk: Mutex::new(Disk { path, unreadable }),
            agents: Arc::new(Agents::none()),
            seen: Mutex::new(Notes::default()),
            callers: Mutex::new(Callers::default()),
            charters: charters::Runtime::default(),
            counts: Mutex::new(CountsDisk {
                path: counts,
                written: 0,
            }),
        }
    }

    /// Takes up what the budgets of the charters in force had used before the proxy
    /// stopped, as last written (save_counts). Counts from another epoch, or of a charter
    /// no longer in force, aren't. A file that can't be read is left, and the budgets
    /// count from zero, as they would with no file: the monitor's check of the record is
    /// the backstop.
    fn take_up_counts(&self) {
        let Some(path) = self.counts.lock().unwrap_or_else(|p| p.into_inner()).path.clone() else {
            return;
        };
        let state = self.current();
        if state.charters.is_empty() {
            return;
        }
        let counts = match std::fs::read(&path) {
            Ok(bytes) => match serde_json::from_slice::<Counts>(&bytes) {
                Ok(counts) => counts,
                Err(e) => {
                    tracing::warn!(path = %path.display(), error = %e, "can't read what the charters' budgets used; counting from zero");
                    return;
                }
            },
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return,
            Err(e) => {
                tracing::warn!(path = %path.display(), error = %e, "can't read what the charters' budgets used; counting from zero");
                return;
            }
        };
        if counts.epoch != state.epoch {
            return;
        }
        let mut used = counts.used;
        used.retain(|id, _| state.charters.iter().any(|c| c.id == *id));
        tracing::info!(charters = used.len(), "took up what the charters' budgets used");
        self.charters.take_up(used);
    }

    /// Writes what each charter's budget has used next to the state, if it changed since it
    /// was last written, so that a restart doesn't hand a run its budget again. Returns
    /// whether it wrote. The proxy calls it every COUNTS_EVERY while running (keep_counts),
    /// and once more when it stops.
    pub fn save_counts(&self) -> std::io::Result<bool> {
        let mut disk = self.counts.lock().unwrap_or_else(|p| p.into_inner());
        let Some(path) = disk.path.clone() else {
            return Ok(false);
        };
        // Read before the counts, so a call counted in between is written next time.
        let changes = self.charters.changes();
        if changes == disk.written {
            return Ok(false);
        }
        let state = self.current();
        if state.unreadable.is_some() || state.uninitialized {
            // Its charters are unknown: what's on disk stays for the state that's replaced.
            return Ok(false);
        }
        let mut used = self.charters.used();
        used.retain(|id, _| state.charters.iter().any(|c| c.id == *id));
        let counts = Counts {
            epoch: state.epoch.clone(),
            used,
        };
        write_atomically(&path, &serde_json::to_vec(&counts)?)?;
        disk.written = changes;
        Ok(true)
    }

    /// Writes what the budgets have used every COUNTS_EVERY, until shutdown resolves. The
    /// last write is the caller's, once nothing more is counted.
    pub async fn keep_counts(self: Arc<Self>, shutdown: impl std::future::Future<Output = ()>) {
        let mut tick = tokio::time::interval(COUNTS_EVERY);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        tokio::pin!(shutdown);
        loop {
            tokio::select! {
                _ = tick.tick() => {}
                _ = &mut shutdown => return,
            }
            let halts = self.clone();
            match tokio::task::spawn_blocking(move || halts.save_counts()).await {
                Ok(Ok(_)) => {}
                Ok(Err(e)) => {
                    tracing::warn!(error = %e, "can't write what the charters' budgets used; a restart would count from what was last written")
                }
                Err(e) => tracing::warn!(error = %e, "writing what the charters' budgets used stopped"),
            }
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

    /// Puts a run charter in force, or replaces one with the same id. What its budget has
    /// used so far stays: the brake puts a charter again when it can't confirm it.
    pub fn put_charter(&self, mut charter: Charter) -> Changed {
        charter.applied_at = STAMP;
        self.change(|state| {
            state.charters.retain(|c| c.id != charter.id);
            state.charters.push(charter);
        })
    }

    /// Whether a charter with this id would fit: the proxy holds at most MAX_CHARTERS, and
    /// putting one again replaces it.
    pub fn charter_fits(&self, id: &str) -> bool {
        let state = self.current();
        state.charters.len() < charters::MAX_CHARTERS || state.charters.iter().any(|c| c.id == id)
    }

    /// Ends a run charter, and forgets what its budget used. Ending one that isn't in force
    /// changes nothing but the version.
    pub fn end_charter(&self, id: &str) -> Changed {
        let changed = self.change(|state| state.charters.retain(|c| c.id != id));
        self.charters.forget(id);
        changed
    }

    /// The charters in force, each with what its budget has used, limit by limit.
    pub fn charters(&self) -> Vec<(Charter, Vec<u64>)> {
        let used = self.charters.used();
        self.current()
            .charters
            .iter()
            .map(|c| {
                let mut counts = used.get(&c.id).cloned().unwrap_or_default();
                counts.resize(c.budget.len(), 0);
                (c.clone(), counts)
            })
            .collect()
    }

    /// Whether a run charter denies a call from caller, the identity the gateway
    /// authenticated, presenting token, to resource, what the call reaches when the hook
    /// knows it. Each charter covers the calls its scope names as a halt's would, but for
    /// those to an agent: the run's own calls. The first that denies is named; a call none
    /// denies is counted against their budgets (charters.rs).
    pub fn charter_deciding(
        &self,
        caller: Option<&str>,
        token: Option<&Token>,
        resource: Option<Resource<'_>>,
        now: u64,
    ) -> Option<charters::Denial> {
        let state = self.current();
        if state.charters.is_empty() {
            return None;
        }
        let who = self.who(caller, token);
        let covering: Vec<&Charter> = state.charters.iter().filter(|c| self.covers(&c.scope, &who)).collect();
        let caller = charters::Caller {
            name: who.identity.map(|i| self.agents.record(i)).unwrap_or("unknown"),
            under: who.chain.clone(),
            unknown: who.unknown,
        };
        let resource = resource.map(|r| match r {
            Resource::Agent(agent) => Resource::Agent(self.agents.named(agent)),
            tool => tool,
        });
        self.charters.decide(
            &covering,
            &caller,
            |a| self.agents.named(a).to_string(),
            resource,
            now / 1_000_000_000,
        )
    }

    /// The rules for one agent's SDK, fetched by a process that says nothing of itself
    /// (memory_rules_for).
    pub fn memory_rules(&self, agent: &str, process: &str) -> MemoryRules {
        self.memory_rules_for(agent, process, Fetcher::default())
    }

    /// The rules for one agent's SDK, noting that this process of the agent has them now:
    /// process is the name the SDK gives its process, empty if it gives none, and fetcher
    /// what the SDK says of it. A state that can't be read, or was never initialized,
    /// refuses every write and serves no entry: whatever it quarantined is unknown, so any
    /// entry might be.
    pub fn memory_rules_for(&self, agent: &str, process: &str, fetcher: Fetcher) -> MemoryRules {
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
            let note = Note {
                version: state.version,
                at: now(),
                hold: fetcher.hold,
                stops: fetcher.stops,
            };
            self.seen
                .lock()
                .unwrap_or_else(|p| p.into_inner())
                .note(agent, process, note);
        }
        let mut halts: Vec<HaltRule> = state
            .halts
            .iter()
            .filter(|h| h.scope.fleet || !h.scope.agents.is_empty())
            .map(|h| HaltRule {
                id: h.id.clone(),
                fleet: h.scope.fleet,
                agents: self.told(&h.scope.agents),
            })
            .collect();
        if let Some(id) = &unknown {
            halts.push(HaltRule {
                id: id.clone(),
                fleet: true,
                agents: vec![],
            });
        }
        MemoryRules {
            version: state.version,
            locked_by,
            hidden_by: unknown,
            quarantines: state.quarantines.clone(),
            halts,
        }
    }

    /// The agents a halt names, as an SDK is told them: as the halt names them, and, for an
    /// identity the agents file lists, its agent's name too, which is what an SDK knows its
    /// agent by.
    fn told(&self, agents: &[String]) -> Vec<String> {
        let mut told: Vec<String> = vec![];
        for named in agents {
            for name in [named.as_str(), self.agents.named(named)] {
                if !told.iter().any(|t| t == name) {
                    told.push(name.to_string());
                }
            }
        }
        told
    }

    /// Each agent whose SDK has fetched its rules, with the version it has: the lowest that
    /// any of its processes still holding it back got, when that process fetched it, and
    /// whether each of them stops halted instances.
    pub fn seen(&self) -> HashMap<String, Seen> {
        self.seen_at(now())
    }

    fn seen_at(&self, now: u64) -> HashMap<String, Seen> {
        self.seen.lock().unwrap_or_else(|p| p.into_inner()).have(now)
    }

    /// Notes a call from identity, the one the gateway authenticated, with the token it
    /// presented, if any. Called for every call the deny lever checks, allowed or not.
    pub fn note_caller(&self, identity: Option<&str>, token: Option<&Token>, at: u64) {
        let Some(identity) = identity.filter(|i| !i.is_empty()) else {
            return;
        };
        let agent = if self.agents.knows(identity) {
            Some(self.agents.named(identity).to_string())
        } else {
            None
        };
        let mut callers = self.callers.lock().unwrap_or_else(|p| p.into_inner());
        let Callers { by_identity, order } = &mut *callers;
        let caller = by_identity.entry(identity.to_string()).or_insert_with(|| {
            order.push_back(identity.to_string());
            Caller {
                agent: None,
                subject: None,
                actors: vec![],
                actors_cut: false,
                token_names_another: false,
                tokens: vec![],
                first_seen: at,
                last_seen: at,
                calls: 0,
            }
        });
        // Named by the agents file, or there's no file to say (Agents::caller).
        let named = self.agents.caller(Some(identity)).is_some();
        caller.agent = agent;
        caller.last_seen = at;
        caller.calls += 1;
        if let Some(token) = token {
            // A caller the file doesn't name is placed under the agent it acts for only by a
            // token naming it as the agent acting (Halts::who). One naming another is said
            // once, in the log.
            let another = token.actors.first().filter(|a| !named && *a != identity);
            if let Some(actor) = another
                && !caller.token_names_another
            {
                tracing::warn!(
                    identity,
                    actor = %actor,
                    "a caller the agents file doesn't list presented a token naming another agent acting: it can't be placed under the agent it acts for, so every agent halt denies it"
                );
            }
            caller.token_names_another = another.is_some();
            caller.subject = Some(token.subject.clone()).filter(|s| !s.is_empty());
            caller.actors = token.actors.clone();
            caller.actors_cut = token.cut;
            if let Some(id) = &token.id {
                caller.tokens.retain(|t| t != id);
                caller.tokens.push(id.clone());
                if caller.tokens.len() > CALLER_TOKENS {
                    caller.tokens.remove(0);
                }
            }
        }
        while order.len() > CALLERS_LIMIT {
            if let Some(oldest) = order.pop_front() {
                by_identity.remove(&oldest);
            }
        }
    }

    /// Every identity the deny lever has seen calling since the proxy started, at most
    /// CALLERS_LIMIT of them.
    pub fn callers(&self) -> HashMap<String, Caller> {
        self.callers
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .by_identity
            .clone()
    }

    /// The callers that are identity, or whose latest token's chain names it: what the
    /// brake reads to tell whether one sub-agent calls under credentials of its own.
    pub fn callers_naming(&self, identity: &str) -> HashMap<String, Caller> {
        let callers = self.callers.lock().unwrap_or_else(|p| p.into_inner());
        callers
            .by_identity
            .iter()
            .filter(|(id, c)| *id == identity || c.actors.iter().any(|a| a == identity))
            .map(|(id, c)| (id.clone(), c.clone()))
            .collect()
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
        for c in next.charters.iter_mut().filter(|c| c.applied_at == STAMP) {
            c.applied_at = at;
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
        self.deciding_with(caller, receiver, None)
    }

    /// The same, for a caller that may have presented a token (token.rs). A halt covers the
    /// call when it names the caller, the receiver, an agent the call is made under, or the
    /// token itself. A call is made under every actor in its token's chain and whom they act
    /// for, its subject (Token::under): halting an agent halts the sub-agents acting for it.
    ///
    /// A caller the identity map doesn't list could be a halted agent, so every agent halt
    /// denies it. But one whose own token's chain passes through an agent the map lists is
    /// a sub-agent acting under that agent, by the identity provider's word: it's covered
    /// only by a halt that names it, its chain or its token, so halting one sub-agent leaves
    /// its siblings be. The token is the caller's own when the agent it names as acting now
    /// is the caller; to act under a chain it isn't part of, an agent would need another
    /// actor's credentials, and with them it is that actor. A chain that was cut could hide
    /// a halted agent in its unknown part, so every agent halt denies it too.
    pub fn deciding_with(&self, caller: Option<&str>, receiver: Option<&str>, token: Option<&Token>) -> Vec<Halt> {
        let state = self.current();
        let who = self.who(caller, token);
        let receiver = receiver.filter(|r| !r.is_empty()).map(|r| self.agents.named(r));
        let mut out: Vec<Halt> = state
            .halts
            .iter()
            .filter(|h| {
                h.scope.fleet
                    || self.covers(&h.scope, &who)
                    || receiver.is_some_and(|r| h.scope.agents.iter().any(|a| self.agents.named(a) == r))
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
                        tokens: vec![],
                    },
                    issued_at: 0,
                    applied_at: 0,
                });
            }
        }
        out
    }
}

/// Who makes a call, as a halt's or a charter's scope is matched against it.
struct Who<'a> {
    identity: Option<&'a str>,
    /// Every agent the call is made under, by its agent's name: the token's actors and whom
    /// they act for (Token::under).
    chain: Vec<&'a str>,
    token_id: Option<&'a str>,
    /// Nobody can name or place the caller, or its token's chain was cut: it could be any
    /// agent.
    unknown: bool,
}

impl Halts {
    fn who<'a>(&'a self, caller: Option<&'a str>, token: Option<&'a Token>) -> Who<'a> {
        let identity = caller.filter(|c| !c.is_empty());
        let chain = token
            .map(|t| t.under().map(|a| self.agents.named(a)).collect())
            .unwrap_or_default();
        let own = token.filter(|t| identity.is_some_and(|i| t.actors.first().is_some_and(|a| a == i)));
        let placed = own.is_some_and(|t| !t.cut && t.under().any(|a| self.agents.knows(a)));
        Who {
            identity,
            chain,
            token_id: token.and_then(|t| t.id.as_deref()),
            unknown: (self.agents.caller(identity).is_none() && !placed) || token.is_some_and(|t| t.cut),
        }
    }

    /// Whether scope covers a call made by who: it names the caller, by its agent's name or,
    /// unlisted, its own identity; an agent the call is made under; or its token. A caller
    /// nobody can name or place could be any agent scope names, so it's covered too.
    fn covers(&self, scope: &Scope, who: &Who<'_>) -> bool {
        let names = |agent: &str| scope.agents.iter().any(|h| self.agents.named(h) == agent);
        who.identity.is_some_and(|i| names(self.agents.named(i)))
            || who.chain.iter().any(|a| names(a))
            || who.token_id.is_some_and(|id| scope.tokens.iter().any(|t| t == id))
            || (who.unknown && !scope.agents.is_empty())
    }
}

// A halt, quarantine or charter's applied_at until the change puts it in force.
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
            tokens: vec![],
        }
    }

    fn fleet() -> Scope {
        Scope {
            fleet: true,
            agents: vec![],
            tokens: vec![],
        }
    }

    fn ids(halts: Vec<Halt>) -> Vec<String> {
        halts.into_iter().map(|h| h.id).collect()
    }

    fn temp_dir() -> PathBuf {
        std::env::temp_dir().join(format!("wake-halts-{}", uuid::Uuid::new_v4()))
    }

    fn charter(id: &str, scope: Scope, never: &str, budget: Vec<charters::Limit>) -> Charter {
        Charter {
            id: id.into(),
            scope,
            expires_at: 4_000_000_000,
            never: never.into(),
            budget,
            issued_at: 1,
            applied_at: 0,
        }
    }

    const NO_DELETES: &str = r#"forbid(principal, action, resource == Wake::Tool::"delete_customer");"#;

    fn one_refund() -> Vec<charters::Limit> {
        vec![charters::Limit {
            tools: vec!["refund".into()],
            calls: 1,
        }]
    }

    #[test]
    fn a_charter_holds_its_runs_calls_and_no_others() {
        let map = br#"{"support": ["0oa1support"], "billing": ["0oa1billing"]}"#;
        let halts = Halts::in_memory().with_agents(Arc::new(Agents::from_json(map).unwrap()));
        let state = halts
            .put_charter(charter("run-1", agents(&[CHILD_1]), NO_DELETES, one_refund()))
            .unwrap();
        assert!(state.charters[0].applied_at > 0, "stamped once in force");
        let at = now();
        let deciding = |caller: Option<&str>, token: Option<&Token>, tool: &str| {
            halts
                .charter_deciding(caller, token, Some(Resource::Tool(tool)), at)
                .map(|d| (d.halt.id, d.why))
        };
        let run = Some(("charter:run-1".to_string(), "never"));
        let child_1 = token("0oa1support", &[CHILD_1], "tok-1");
        assert_eq!(deciding(Some(CHILD_1), Some(&child_1), "delete_customer"), run);
        assert_eq!(deciding(Some(CHILD_1), Some(&child_1), "refund"), None);
        assert_eq!(
            deciding(Some(CHILD_1), Some(&child_1), "refund").map(|d| d.1),
            Some("budget")
        );
        // A sub-agent the run started acts under it, so it's the run's too.
        let grandchild = token("0oa1support", &["support/researcher/fetch#toolu_9", CHILD_1], "tok-9");
        assert_eq!(
            deciding(
                Some("support/researcher/fetch#toolu_9"),
                Some(&grandchild),
                "delete_customer"
            ),
            run
        );
        // Its sibling, its parent and billing aren't held to it.
        let child_2 = token("0oa1support", &[CHILD_2], "tok-2");
        assert_eq!(deciding(Some(CHILD_2), Some(&child_2), "delete_customer"), None);
        let support = token("0oa1support", &[], "tok-0");
        assert_eq!(deciding(Some("0oa1support"), Some(&support), "delete_customer"), None);
        assert_eq!(deciding(Some("0oa1billing"), None, "delete_customer"), None);
        // A caller nobody can name or place could be the run's.
        assert_eq!(deciding(None, None, "delete_customer"), run);
        assert_eq!(
            deciding(Some("spiffe://wake.local/agent/stranger"), None, "delete_customer"),
            run
        );
        // But it isn't counted against the run's budget, so it can't use it up.
        assert_eq!(deciding(None, None, "refund"), None);
        assert_eq!(halts.charters()[0].1, vec![1]);

        // Put again, as the brake does when it can't confirm it, it keeps what it used.
        halts
            .put_charter(charter("run-1", agents(&[CHILD_1]), NO_DELETES, one_refund()))
            .unwrap();
        assert_eq!(halts.charters()[0].1, vec![1]);
        assert_eq!(
            deciding(Some(CHILD_1), Some(&child_1), "refund").map(|d| d.1),
            Some("budget")
        );

        // A run named by a token issued for it: whoever presents it. Expired, it denies
        // every call that token makes, whatever the call reaches.
        let mut expired = charter(
            "run-2",
            Scope {
                tokens: vec!["tok-run".into()],
                ..Default::default()
            },
            "",
            vec![],
        );
        expired.expires_at = 1;
        halts.put_charter(expired).unwrap();
        let with_run_token = token("user-42", &["0oa1billing"], "tok-run");
        assert_eq!(
            halts
                .charter_deciding(Some("0oa1billing"), Some(&with_run_token), None, at)
                .map(|d| (d.halt.id, d.why)),
            Some(("charter:run-2".into(), "expired"))
        );
        assert_eq!(halts.charter_deciding(Some("0oa1billing"), None, None, at), None);

        // Ended, a charter holds nothing, and a new one with its id starts afresh.
        halts.end_charter("run-1").unwrap();
        assert_eq!(deciding(Some(CHILD_1), Some(&child_1), "delete_customer"), None);
        halts
            .put_charter(charter("run-1", agents(&[CHILD_1]), NO_DELETES, one_refund()))
            .unwrap();
        assert_eq!(deciding(Some(CHILD_1), Some(&child_1), "refund"), None);
    }

    #[test]
    fn a_proxy_holds_so_many_charters() {
        let halts = Halts::in_memory();
        for i in 0..charters::MAX_CHARTERS {
            halts
                .put_charter(charter(&format!("run-{i}"), agents(&[CHILD_1]), "", vec![]))
                .unwrap();
        }
        assert!(!halts.charter_fits("run-new"));
        assert!(halts.charter_fits("run-7"), "put again, it replaces itself");
        halts.end_charter("run-7").unwrap();
        assert!(halts.charter_fits("run-new"));
    }

    #[test]
    fn a_charter_is_kept_on_disk_as_a_halt_is() {
        let dir = temp_dir();
        std::fs::create_dir_all(&dir).unwrap();
        Halts::init(&dir).unwrap();
        let halts = Halts::load(&dir);
        halts
            .put_charter(charter("run-1", agents(&["support"]), NO_DELETES, one_refund()))
            .unwrap();
        let again = Halts::load(&dir);
        assert_eq!(again.state().charters, halts.state().charters);
        assert!(
            again
                .charter_deciding(Some("support"), None, Some(Resource::Tool("delete_customer")), now())
                .is_some()
        );
        std::fs::remove_dir_all(&dir).unwrap();
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

    /// What the gateway says about a token: whom it acts for, and the agents acting, the
    /// one acting now first.
    fn token(subject: &str, actors: &[&str], id: &str) -> Token {
        Token {
            subject: subject.into(),
            actors: actors.iter().map(|a| a.to_string()).collect(),
            id: Some(id.into()),
            ..Default::default()
        }
    }

    const CHILD_1: &str = "support/researcher#toolu_1";
    const CHILD_2: &str = "support/researcher#toolu_2";

    #[test]
    fn a_halt_on_an_agent_covers_the_sub_agents_acting_for_it() {
        let halts = Halts::in_memory();
        halts.put("h1", agents(&["support"]), 1).unwrap();
        // Support delegated to the child for a user: support is in the chain.
        let for_user = token("user-42", &[CHILD_1, "support"], "tok-1");
        assert_eq!(ids(halts.deciding_with(Some(CHILD_1), None, Some(&for_user))), ["h1"]);
        // Support delegated its own token: support is whom the child acts for.
        let for_support = token("support", &[CHILD_2], "tok-2");
        assert_eq!(
            ids(halts.deciding_with(Some(CHILD_2), None, Some(&for_support))),
            ["h1"]
        );
        // Further down, a grandchild acting under the child is covered too.
        let grandchild = token(
            "user-42",
            &["support/researcher/fetch#toolu_9", CHILD_1, "support"],
            "tok-3",
        );
        assert_eq!(
            ids(halts.deciding_with(Some("support/researcher/fetch#toolu_9"), None, Some(&grandchild))),
            ["h1"]
        );
        // Over mTLS, a caller presenting a token issued to support acts as support.
        let supports_own = token("support", &[], "tok-5");
        assert_eq!(
            ids(halts.deciding_with(Some("spiffe://wake.local/agent/other"), None, Some(&supports_own))),
            ["h1"]
        );
        // An agent outside support's chain goes on, with or without a token.
        let billing = token("user-42", &["billing"], "tok-4");
        assert!(halts.deciding_with(Some("billing"), None, Some(&billing)).is_empty());
        assert!(halts.deciding(Some("billing"), None).is_empty());
        // Without the token, the child is just a caller of its own name: the token is what
        // places it under support.
        assert!(halts.deciding(Some(CHILD_1), None).is_empty());
    }

    #[test]
    fn halting_one_sub_agent_leaves_its_siblings_and_its_parent_be() {
        // The map lists the agents, by the subjects their own tokens carry; sub-agents
        // aren't in it.
        let map = br#"{"support": ["0oa1support"], "billing": ["0oa1billing"]}"#;
        let halts = Halts::in_memory().with_agents(Arc::new(Agents::from_json(map).unwrap()));
        let child_1 = token("0oa1support", &[CHILD_1], "tok-1");
        let child_2 = token("0oa1support", &[CHILD_2], "tok-2");
        let support = token("0oa1support", &[], "tok-0");
        halts.put("h1", agents(&[CHILD_1]), 1).unwrap();
        assert_eq!(ids(halts.deciding_with(Some(CHILD_1), None, Some(&child_1))), ["h1"]);
        assert!(
            halts.deciding_with(Some(CHILD_2), None, Some(&child_2)).is_empty(),
            "its sibling acts under support, by the identity provider's word"
        );
        assert!(
            halts
                .deciding_with(Some("0oa1support"), None, Some(&support))
                .is_empty()
        );
        assert!(halts.deciding(Some("0oa1billing"), Some("support")).is_empty());

        // The rule for callers nobody can name still holds for anyone not placed.
        assert_eq!(ids(halts.deciding(Some(CHILD_2), None)), ["h1"], "no token");
        assert_eq!(ids(halts.deciding(None, None)), ["h1"], "no identity");
        assert_eq!(
            ids(halts.deciding_with(Some("intruder"), None, Some(&child_2))),
            ["h1"],
            "a token that names another as acting isn't the caller's own"
        );
        let unlisted = token("user-42", &["scraper#run_1", "scraper"], "tok-5");
        assert_eq!(
            ids(halts.deciding_with(Some("scraper#run_1"), None, Some(&unlisted))),
            ["h1"],
            "a chain through no listed agent places nobody"
        );
        let cut = Token {
            cut: true,
            ..child_2.clone()
        };
        assert_eq!(
            ids(halts.deciding_with(Some(CHILD_2), None, Some(&cut))),
            ["h1"],
            "the halted agent could be in the part that was cut"
        );
        let listed_cut = Token {
            cut: true,
            ..token("0oa1support", &["0oa1billing"], "tok-6")
        };
        assert_eq!(
            ids(halts.deciding_with(Some("0oa1billing"), None, Some(&listed_cut))),
            ["h1"],
            "even for a caller the map lists"
        );

        // Halting the parent, by its name, halts both children and leaves billing be.
        halts.lift("h1").unwrap();
        halts.put("h2", agents(&["support"]), 2).unwrap();
        assert_eq!(ids(halts.deciding_with(Some(CHILD_1), None, Some(&child_1))), ["h2"]);
        assert_eq!(ids(halts.deciding_with(Some(CHILD_2), None, Some(&child_2))), ["h2"]);
        assert_eq!(
            ids(halts.deciding_with(Some("0oa1support"), None, Some(&support))),
            ["h2"]
        );
        let billing = token("0oa1billing", &[], "tok-7");
        assert!(
            halts
                .deciding_with(Some("0oa1billing"), None, Some(&billing))
                .is_empty()
        );
    }

    #[test]
    fn a_halt_by_token_stops_that_token_alone() {
        let map = br#"{"support": ["0oa1support"]}"#;
        let halts = Halts::in_memory().with_agents(Arc::new(Agents::from_json(map).unwrap()));
        let scope = Scope {
            tokens: vec!["tok-1".into()],
            ..Default::default()
        };
        assert!(!scope.is_empty());
        halts.put("t1", scope, 1).unwrap();
        let child_1 = token("0oa1support", &[CHILD_1], "tok-1");
        assert_eq!(ids(halts.deciding_with(Some(CHILD_1), None, Some(&child_1))), ["t1"]);
        assert_eq!(
            ids(halts.deciding_with(Some("anyone"), None, Some(&child_1))),
            ["t1"],
            "whoever presents it"
        );
        let renewed = token("0oa1support", &[CHILD_1], "tok-2");
        assert!(
            halts.deciding_with(Some(CHILD_1), None, Some(&renewed)).is_empty(),
            "another token for the same child isn't the one halted"
        );
        // A token halt names no agent, so a caller nobody can name isn't denied by it.
        assert!(halts.deciding(None, None).is_empty());
        assert!(halts.deciding(Some("intruder"), None).is_empty());
        assert!(halts.deciding_with(Some(CHILD_1), None, None).is_empty());
    }

    #[test]
    fn a_scope_with_tokens_reads_and_writes_them() {
        let scope: Scope = serde_json::from_str(r#"{"tokens": ["tok-1"]}"#).unwrap();
        assert_eq!(scope.tokens, ["tok-1"]);
        assert!(!scope.fleet && scope.agents.is_empty() && !scope.is_empty());
        assert_eq!(
            serde_json::to_string(&scope).unwrap(),
            r#"{"fleet":false,"agents":[],"tokens":["tok-1"]}"#
        );
        // A state written before tokens existed reads as it was, and writes as it was.
        let scope: Scope = serde_json::from_str(r#"{"agents": ["billing"]}"#).unwrap();
        assert!(scope.tokens.is_empty());
        assert_eq!(
            serde_json::to_string(&scope).unwrap(),
            r#"{"fleet":false,"agents":["billing"]}"#
        );
        assert!(serde_json::from_str::<Scope>("{}").unwrap().is_empty());
    }

    #[test]
    fn notes_each_caller_with_its_chain_and_latest_tokens() {
        let map = br#"{"support": ["0oa1support"]}"#;
        let halts = Halts::in_memory().with_agents(Arc::new(Agents::from_json(map).unwrap()));
        halts.note_caller(None, None, 1);
        halts.note_caller(Some(""), Some(&token("u", &[], "x")), 1);
        assert!(halts.callers().is_empty(), "a call with no identity names nobody");

        halts.note_caller(Some(CHILD_1), Some(&token("0oa1support", &[CHILD_1], "tok-1")), 10);
        halts.note_caller(Some(CHILD_1), Some(&token("0oa1support", &[CHILD_1], "tok-2")), 20);
        halts.note_caller(Some(CHILD_1), Some(&token("0oa1support", &[CHILD_1], "tok-1")), 30);
        halts.note_caller(Some("0oa1support"), None, 40);
        let callers = halts.callers();
        assert_eq!(
            callers[CHILD_1],
            Caller {
                agent: None,
                subject: Some("0oa1support".into()),
                actors: vec![CHILD_1.into()],
                actors_cut: false,
                token_names_another: false,
                tokens: vec!["tok-2".into(), "tok-1".into()],
                first_seen: 10,
                last_seen: 30,
                calls: 3,
            }
        );
        assert_eq!(callers["0oa1support"].agent.as_deref(), Some("support"));
        assert_eq!(
            serde_json::to_value(&callers["0oa1support"]).unwrap(),
            serde_json::json!({"agent": "support", "first_seen": 40, "last_seen": 40, "calls": 1})
        );

        let cut = Token {
            cut: true,
            ..token("user-42", &["deep#9"], "tok-deep")
        };
        halts.note_caller(Some("deep#9"), Some(&cut), 45);
        assert!(halts.callers()["deep#9"].actors_cut);
        assert_eq!(
            serde_json::to_value(&halts.callers()["deep#9"]).unwrap()["actors_cut"],
            true
        );

        // A call without a token keeps what the last token said.
        halts.note_caller(Some(CHILD_1), None, 50);
        assert_eq!(halts.callers()[CHILD_1].tokens, ["tok-2", "tok-1"]);
        for i in 0..20 {
            let renewed = token("0oa1support", &[CHILD_1], &format!("n{i}"));
            halts.note_caller(Some(CHILD_1), Some(&renewed), 60);
        }
        let tokens = &halts.callers()[CHILD_1].tokens;
        assert_eq!(tokens.len(), CALLER_TOKENS);
        assert_eq!(tokens.last().map(String::as_str), Some("n19"), "newest last");
    }

    #[test]
    fn the_callers_naming_an_identity_are_it_and_those_acting_under_it() {
        let halts = Halts::in_memory();
        halts.note_caller(Some(CHILD_1), Some(&token("support", &[CHILD_1], "tok-1")), 1);
        let grandchild = "support/researcher/fetch#toolu_9";
        halts.note_caller(
            Some(grandchild),
            Some(&token("support", &[grandchild, CHILD_1], "tok-9")),
            2,
        );
        halts.note_caller(Some(CHILD_2), Some(&token("support", &[CHILD_2], "tok-2")), 3);
        halts.note_caller(Some("support"), None, 4);
        let mut named: Vec<String> = halts.callers_naming(CHILD_1).into_keys().collect();
        named.sort();
        assert_eq!(named, [CHILD_1, grandchild]);
        assert!(halts.callers_naming("support/researcher#toolu_7").is_empty());
        assert_eq!(
            halts.callers_naming("support").len(),
            1,
            "the subject isn't a chain's actor"
        );
    }

    #[test]
    fn the_callers_noted_are_bounded() {
        let halts = Halts::in_memory();
        for i in 0..=CALLERS_LIMIT {
            halts.note_caller(Some(&format!("agent-{i}")), None, i as u64);
        }
        let callers = halts.callers();
        assert_eq!(callers.len(), CALLERS_LIMIT);
        assert!(!callers.contains_key("agent-0"), "the one seen first goes first");
        assert!(callers.contains_key(&format!("agent-{CALLERS_LIMIT}")));
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

        let billing = halts.memory_rules("billing", "");
        assert_eq!(billing.locked_by.as_deref(), Some("q1"));
        assert_eq!(billing.quarantines[0].selection.origins, vec!["tool:fetch_page"]);
        let support = halts.memory_rules("support", "");
        assert_eq!(support.locked_by, None, "only billing's writes are refused");
        assert_eq!(
            support.quarantines.len(),
            1,
            "every agent hides the quarantined entries"
        );
        assert_eq!(halts.memory_rules("", "").locked_by, None);

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
        assert_eq!(halts.memory_rules("support", "").locked_by.as_deref(), Some("q2"));
        halts.release("q1").unwrap();
        let state = halts.release("q2").unwrap();
        assert_eq!((state.version, state.quarantines.len()), (4, 0));
        assert_eq!(halts.memory_rules("billing", "").locked_by, None);
    }

    #[test]
    fn the_notes_of_who_fetched_are_bounded() {
        let mut notes = Notes::default();
        let t = 1_000 * PROCESS_LIVE;
        let last = SEEN_LIMIT + HELD_LIMIT;
        // Every agent fetched just now. Past SEEN_LIMIT the oldest notes are held back, and
        // past HELD_LIMIT, the next goes into the floor, which holds every agent back to it.
        for i in 0..=last {
            let version = if i == HELD_LIMIT { 1 } else { 2 };
            notes.note(&format!("agent-{i}"), "", fetch(version, t));
        }
        assert_eq!(notes.order.len(), SEEN_LIMIT);
        assert_eq!(notes.held, HELD_LIMIT);
        assert_eq!(notes.by_agent.len(), last, "the agent behind the floor went");
        let seen = notes.have(t + 1);
        assert_eq!(seen.len(), last);
        assert!(
            seen.values().all(|s| s.version == 1),
            "the floor holds every agent back"
        );
        // Once their holds end, the notes held back and the floor go.
        let seen = notes.have(t + PROCESS_LIVE);
        assert_eq!(seen.len(), SEEN_LIMIT);
        assert!(seen.values().all(|s| s.version == 2));
        assert!(seen.contains_key(&format!("agent-{last}")) && !seen.contains_key("agent-0"));
        assert_eq!((notes.held, notes.floor), (0, None));
    }

    #[test]
    fn a_halt_is_confirmed_once_every_process_of_its_agent_has_it() {
        let halts = Halts::in_memory();
        halts.memory_rules("support", "41.a1");
        halts.memory_rules("support", "42.b2");
        let state = halts.put("h1", agents(&["support"]), 1).unwrap();
        halts.memory_rules("support", "41.a1");
        assert!(
            halts.seen()["support"].version < state.version,
            "one process having the halt doesn't confirm it while another running support hasn't fetched it"
        );
        halts.memory_rules("support", "42.b2");
        assert_eq!(halts.seen()["support"].version, state.version);
    }

    /// A fetch by an SDK that says nothing of its process.
    fn fetch(version: u64, at: u64) -> Note {
        Note {
            version,
            at,
            hold: PROCESS_LIVE,
            stops: false,
        }
    }

    fn seen(version: u64, at: u64) -> Seen {
        Seen {
            version,
            at,
            stops: false,
        }
    }

    #[test]
    fn a_process_silent_a_minute_no_longer_holds_its_agent_back() {
        let mut notes = Notes::default();
        let t = 1_000 * PROCESS_LIVE;
        notes.note("support", "old", fetch(1, t));
        notes.note("support", "new", fetch(2, t + 10));
        assert_eq!(notes.have(t + 11)["support"], seen(1, t));
        // Stopped, or refusing every call on rules this old.
        assert_eq!(notes.have(t + PROCESS_LIVE)["support"], seen(2, t + 10));
        // With none still fetching, the agent has what it last fetched.
        assert_eq!(notes.have(t + 10 * PROCESS_LIVE)["support"], seen(2, t + 10));
    }

    #[test]
    fn a_process_holds_its_agent_back_while_its_sdk_acts_on_its_rules() {
        let secs = 1_000_000_000;
        let hold = |ttl: Option<&str>| Fetcher::new(ttl, false).hold / secs;
        assert_eq!(
            hold(None),
            60,
            "an SDK that doesn't say acts on its rules for 30 seconds"
        );
        assert_eq!(hold(Some("30")), 60);
        assert_eq!(hold(Some("75.5")), 85);
        assert_eq!(hold(Some("120")), 130);
        assert_eq!(hold(Some("3600")), 600, "at most");
        for unreadable in ["", "soon", "-1", "NaN", "inf"] {
            assert_eq!(hold(Some(unreadable)), 600, "{unreadable:?} could be anything");
        }
        // A process that acts on its rules for two minutes holds its agent back past one.
        let mut notes = Notes::default();
        let t = 1_000 * PROCESS_LIVE;
        let slow = Note {
            hold: Fetcher::new(Some("120"), false).hold,
            ..fetch(1, t)
        };
        notes.note("support", "slow", slow);
        notes.note("support", "fast", fetch(2, t + 1));
        assert_eq!(notes.have(t + 129 * secs)["support"].version, 1);
        assert_eq!(notes.have(t + 130 * secs)["support"].version, 2);
    }

    #[test]
    fn an_agent_stops_halted_instances_once_each_process_holding_it_back_does() {
        let mut notes = Notes::default();
        let t = 1_000 * PROCESS_LIVE;
        let stopping = |version, at| Note {
            stops: true,
            ..fetch(version, at)
        };
        notes.note("support", "a", stopping(1, t));
        notes.note("support", "b", fetch(2, t + 1));
        assert!(!notes.have(t + 2)["support"].stops, "b doesn't say it stops them");
        notes.note("support", "b", stopping(2, t + 3));
        let both = notes.have(t + 4)["support"];
        assert_eq!(
            serde_json::to_value(both).unwrap(),
            serde_json::json!({"version": 1, "at": t, "stops": true})
        );
        // Said with each fetch: a process whose integration no longer says so doesn't.
        notes.note("support", "a", fetch(2, t + 5));
        let one = notes.have(t + 6)["support"];
        assert_eq!(
            serde_json::to_value(one).unwrap(),
            serde_json::json!({"version": 2, "at": t + 3}),
            "left out when not"
        );
        // With none holding it back, what its latest fetch said.
        assert!(!notes.have(t + 10 * PROCESS_LIVE)["support"].stops);
    }

    /// Seconds from a halt until support's version reaches it, with processes of support each
    /// fetching every 2 seconds, spread over each round, and the halt landing after two
    /// minutes of them.
    fn confirmed_after(processes: u64) -> Option<u64> {
        let secs = 1_000_000_000;
        let t = 1_000 * PROCESS_LIVE;
        let halted = 60;
        let mut notes = Notes::default();
        for round in 0..200 {
            let version = if round >= halted { 2 } else { 1 };
            let start = t + round * 2 * secs;
            for p in 0..processes {
                let at = start + p * (2 * secs / processes);
                notes.note("support", &format!("p{p}"), fetch(version, at));
            }
            let end = start + 2 * secs - 1;
            if round >= halted && notes.have(end)["support"].version == 2 {
                return Some((end - (t + halted * 2 * secs)) / secs);
            }
        }
        None
    }

    #[test]
    fn more_processes_than_an_agent_keeps_confirm_a_halt_once_each_has_it() {
        let limit = PROCESSES_LIMIT as u64;
        for processes in [limit, limit + 1, 2 * limit] {
            assert_eq!(confirmed_after(processes), Some(1), "{processes} processes");
        }
    }

    #[test]
    fn a_note_going_never_makes_its_agent_look_further_along() {
        let mut notes = Notes::default();
        let t = 1_000 * PROCESS_LIVE;
        notes.note("support", "p0", fetch(1, t));
        for i in 1..=PROCESSES_LIMIT as u64 {
            notes.note("support", &format!("p{i}"), fetch(2, t + i));
        }
        assert_eq!(notes.by_agent["support"].by_process.len(), PROCESSES_LIMIT);
        assert!(
            !notes.by_agent["support"].by_process.contains_key("p0"),
            "the process whose hold ends first went"
        );
        assert_eq!(notes.order.len(), PROCESSES_LIMIT, "and its place in line");
        assert_eq!(notes.have(t + 100)["support"].version, 1, "it still holds support back");
        // Fetching again, it holds support back by that fetch alone, and another goes.
        notes.note("support", "p0", fetch(2, t + 200));
        assert_eq!(notes.have(t + 201)["support"].version, 2);
        assert_eq!((notes.held, notes.by_agent["support"].held.len()), (1, 1));

        // Past the bound on every note, the oldest goes, with the same care.
        let mut notes = Notes::default();
        notes.note("billing", "x", fetch(1, t));
        notes.note("billing", "y", fetch(2, t + 1));
        for i in 0..SEEN_LIMIT - 1 {
            notes.note(&format!("agent-{i}"), "", fetch(2, t + 2));
        }
        assert!(!notes.by_agent["billing"].by_process.contains_key("x"));
        assert_eq!(notes.have(t + 3)["billing"].version, 1);
        assert_eq!(notes.order.len(), SEEN_LIMIT);
        // Billing's last note goes too, and billing stays while x still holds it back: y
        // fetching again doesn't make it look as if every process of billing had version 2.
        notes.note("other", "z", fetch(2, t + 3));
        assert!(notes.by_agent["billing"].by_process.is_empty());
        notes.note("billing", "y", fetch(2, t + 4));
        assert_eq!(notes.have(t + 5)["billing"], seen(1, t));
        assert_eq!(notes.have(t + PROCESS_LIVE)["billing"], seen(2, t + 4));
    }

    #[test]
    fn a_caller_whose_token_names_another_agent_acting_is_marked() {
        let map = br#"{"support": ["0oa1support"]}"#;
        let halts = Halts::in_memory().with_agents(Arc::new(Agents::from_json(map).unwrap()));
        let svid = "spiffe://wake.local/agent/support/refunder";
        // Its identity provider named it otherwise in the token's act: it can't be placed.
        halts.note_caller(Some(svid), Some(&token("0oa1support", &["refunder-7"], "tok-1")), 1);
        assert!(halts.callers()[svid].token_names_another);
        // Named by its SPIFFE ID, it is.
        halts.note_caller(Some(svid), Some(&token("0oa1support", &[svid], "tok-2")), 2);
        assert!(!halts.callers()[svid].token_names_another);
        // An identity the file names is named by it, whatever its token says.
        halts.note_caller(Some("0oa1support"), Some(&token("user-1", &["someone"], "tok-3")), 3);
        assert!(!halts.callers()["0oa1support"].token_names_another);
    }

    fn budget_of(calls: u64) -> Vec<charters::Limit> {
        vec![charters::Limit { tools: vec![], calls }]
    }

    fn call(halts: &Halts) -> Option<&'static str> {
        halts
            .charter_deciding(Some("support"), None, Some(Resource::Tool("refund")), now())
            .map(|d| d.why)
    }

    #[test]
    fn what_a_budget_used_survives_a_restart() {
        let dir = temp_dir();
        std::fs::create_dir_all(&dir).unwrap();
        Halts::init(&dir).unwrap();
        let halts = Halts::load(&dir);
        assert!(!halts.save_counts().unwrap(), "nothing counted, nothing written");
        halts
            .put_charter(charter("run-1", agents(&["support"]), "", budget_of(3)))
            .unwrap();
        assert_eq!((call(&halts), call(&halts)), (None, None));
        assert!(halts.save_counts().unwrap());
        assert!(!halts.save_counts().unwrap(), "nothing new to write");

        let restarted = Halts::load(&dir);
        assert_eq!(restarted.charters()[0].1, vec![2], "it counts on from what was written");
        assert_eq!((call(&restarted), call(&restarted)), (None, Some("budget")));

        // Ended, a charter's counts leave the file too.
        restarted.end_charter("run-1").unwrap();
        assert!(restarted.save_counts().unwrap());
        restarted
            .put_charter(charter("run-1", agents(&["support"]), "", budget_of(3)))
            .unwrap();
        assert_eq!(Halts::load(&dir).charters()[0].1, vec![0]);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn counts_from_another_state_or_unreadable_are_not_taken_up() {
        let dir = temp_dir();
        std::fs::create_dir_all(&dir).unwrap();
        Halts::init(&dir).unwrap();
        let halts = Halts::load(&dir);
        halts
            .put_charter(charter("run-1", agents(&["support"]), "", budget_of(3)))
            .unwrap();
        let other = Counts {
            epoch: "another-state".into(),
            used: HashMap::from([("run-1".into(), vec![3])]),
        };
        std::fs::write(dir.join(COUNTS_FILE), serde_json::to_vec(&other).unwrap()).unwrap();
        assert_eq!(Halts::load(&dir).charters()[0].1, vec![0], "another epoch's runs");
        std::fs::write(dir.join(COUNTS_FILE), b"{not json").unwrap();
        let again = Halts::load(&dir);
        assert_eq!(again.charters()[0].1, vec![0], "counted from zero, as with no file");
        assert_eq!(again.state().charters.len(), 1, "the state itself is untouched");
        std::fs::remove_dir_all(&dir).unwrap();
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
    fn the_memory_rules_say_which_agents_and_sub_agents_are_halted() {
        let halts = Halts::in_memory();
        assert_eq!(halts.memory_rules("support", "").halts, [], "none, said");
        halts.put("h-sub", agents(&[CHILD_1]), 1).unwrap();
        halts.put("h-fleet", fleet(), 2).unwrap();
        let tokens_only = Scope {
            tokens: vec!["tok-1".into()],
            ..Default::default()
        };
        halts.put("h-token", tokens_only, 3).unwrap();
        let rules = halts.memory_rules("support", "");
        assert_eq!(
            rules.halts,
            [
                HaltRule {
                    id: "h-sub".into(),
                    fleet: false,
                    agents: vec![CHILD_1.into()]
                },
                HaltRule {
                    id: "h-fleet".into(),
                    fleet: true,
                    agents: vec![]
                },
            ],
            "a token halt names no agent, and no token is told"
        );
        let json = serde_json::to_value(&rules).unwrap();
        assert_eq!(
            json["halts"][0],
            serde_json::json!({"id": "h-sub", "agents": [CHILD_1]})
        );
        assert_eq!(json["halts"][1], serde_json::json!({"id": "h-fleet", "fleet": true}));
        assert!(!json.to_string().contains("tok-1"));
    }

    #[test]
    fn the_memory_rules_name_an_agent_a_halt_names_by_identity_as_its_sdk_knows_it() {
        let map = br#"{"billing": ["spiffe://wake.local/agent/billing"]}"#;
        let halts = Halts::in_memory().with_agents(Arc::new(Agents::from_json(map).unwrap()));
        halts
            .put("h1", agents(&["spiffe://wake.local/agent/billing", CHILD_1]), 1)
            .unwrap();
        assert_eq!(
            halts.memory_rules("billing", "").halts[0].agents,
            ["spiffe://wake.local/agent/billing", "billing", CHILD_1]
        );
        halts
            .put("h1", agents(&["billing", "spiffe://wake.local/agent/billing"]), 2)
            .unwrap();
        assert_eq!(
            halts.memory_rules("billing", "").halts[0].agents,
            ["billing", "spiffe://wake.local/agent/billing"],
            "each once"
        );
    }

    #[test]
    fn a_state_never_initialized_or_lost_halts_everything() {
        let dir = temp_dir();
        let halts = Halts::load(&dir);
        assert!(halts.state().uninitialized);
        assert_eq!(ids(halts.deciding(Some("anyone"), None)), [UNINITIALIZED]);
        let rules = halts.memory_rules("billing", "");
        assert_eq!(rules.locked_by.as_deref(), Some(UNINITIALIZED));
        assert_eq!(
            rules.halts,
            [HaltRule {
                id: UNINITIALIZED.into(),
                fleet: true,
                agents: vec![]
            }],
            "an SDK stops every agent in its process too"
        );
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
        assert_eq!(restarted.memory_rules("billing", "").hidden_by, None);
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
        let rules = halts.memory_rules("billing", "");
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
