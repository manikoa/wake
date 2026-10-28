//! What recording may hold in memory. The processor shares its process with the deny lever,
//! so a flood of large MCP calls or A2A messages mustn't grow it until it's killed and
//! takes the deny down with it. Every queue is bounded by bytes as well as by count, and
//! each payload by MAX_CAPTURE. Past a bound, recording drops and counts; it never waits.

use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use tokio::sync::mpsc;

use crate::canonical::{Event, Value};
use crate::export::Counters;

/// How much of an MCP call's params or result, or an A2A body, goes into an event's
/// payload. Beyond it, the payload keeps a prefix, marked truncated.
pub const MAX_CAPTURE: usize = 1 << 20;

/// Bytes that queues and pending calls may hold at once.
pub struct Budget {
    used: AtomicUsize,
    limit: usize,
}

impl Budget {
    pub fn new(limit: usize) -> Arc<Self> {
        Arc::new(Budget {
            used: AtomicUsize::new(0),
            limit,
        })
    }

    /// Takes bytes from the budget, or None when they don't fit.
    pub fn take(self: &Arc<Self>, bytes: usize) -> Option<Permit> {
        let mut used = self.used.load(Ordering::Acquire);
        loop {
            let next = used.checked_add(bytes).filter(|&n| n <= self.limit)?;
            match self
                .used
                .compare_exchange_weak(used, next, Ordering::AcqRel, Ordering::Acquire)
            {
                Ok(_) => break,
                Err(now) => used = now,
            }
        }
        Some(Permit {
            budget: self.clone(),
            bytes,
        })
    }

    pub fn used(&self) -> usize {
        self.used.load(Ordering::Acquire)
    }
}

/// Bytes taken from a budget, given back when it's dropped.
#[derive(Debug)]
pub struct Permit {
    budget: Arc<Budget>,
    bytes: usize,
}

impl Permit {
    pub fn bytes(&self) -> usize {
        self.bytes
    }
}

impl Drop for Permit {
    fn drop(&mut self) {
        self.budget.used.fetch_sub(self.bytes, Ordering::AcqRel);
    }
}

impl std::fmt::Debug for Budget {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Budget({}/{})", self.used(), self.limit)
    }
}

/// About how many bytes an event holds: its name and attributes, and what the signer adds.
pub fn weight(event: &Event) -> usize {
    const SIGNED: usize = 2048;
    let value = |v: &Value| match v {
        Value::Str(s) => s.len(),
        Value::Strs(list) => list.iter().map(|s| s.len() + 8).sum(),
        Value::Int(_) | Value::Bool(_) => 8,
    };
    SIGNED
        + event.name.len()
        + event
            .attributes
            .iter()
            .map(|(k, v)| k.len() + value(v) + 16)
            .sum::<usize>()
}

/// An event on its way to the collector, with the bytes it holds.
#[derive(Debug)]
pub struct Queued {
    pub event: Event,
    pub permit: Permit,
}

/// Where every hook sends the events it records: one queue to the signer, bounded by count
/// and by bytes. An event that doesn't fit is dropped and counted.
#[derive(Clone)]
pub struct Events {
    tx: mpsc::Sender<Queued>,
    budget: Arc<Budget>,
    counters: Arc<Counters>,
}

impl Events {
    /// A queue of at most count events holding at most budget's bytes, shared with the
    /// stages after it, which pass each event's permit along until it's sent.
    pub fn channel(count: usize, budget: Arc<Budget>, counters: Arc<Counters>) -> (Self, mpsc::Receiver<Queued>) {
        let (tx, rx) = mpsc::channel(count);
        (Events { tx, budget, counters }, rx)
    }

    pub fn send(&self, event: Event) {
        let sent = self
            .budget
            .take(weight(&event))
            .map(|permit| self.tx.try_send(Queued { event, permit }).is_ok())
            .unwrap_or(false);
        if !sent {
            self.counters.export_dropped.fetch_add(1, Ordering::Relaxed);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_budget_never_lends_more_than_its_limit() {
        let budget = Budget::new(100);
        let a = budget.take(60).unwrap();
        assert!(budget.take(50).is_none());
        let b = budget.take(40).unwrap();
        assert_eq!(budget.used(), 100);
        drop(a);
        assert_eq!(budget.used(), 40);
        assert!(budget.take(usize::MAX).is_none(), "no overflow");
        drop(b);
        assert_eq!(budget.used(), 0);
    }

    #[test]
    fn an_event_over_the_budget_is_dropped_and_counted() {
        let counters = Arc::new(Counters::default());
        let budget = Budget::new(64 << 10);
        let (events, mut rx) = Events::channel(8, budget.clone(), counters.clone());
        let mut small = Event::default();
        small.set("wake.payload", Value::Str("{}".into()));
        let mut big = Event::default();
        big.set("wake.payload", Value::Str("x".repeat(100 << 10)));
        events.send(small);
        events.send(big);
        let queued = rx.try_recv().unwrap();
        assert!(rx.try_recv().is_err());
        assert_eq!(Counters::get(&counters.export_dropped), 1);
        assert_eq!(budget.used(), queued.permit.bytes());
        drop(queued);
        assert_eq!(budget.used(), 0, "sent, so given back");
    }
}
