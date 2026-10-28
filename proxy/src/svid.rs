//! The processor's own X.509-SVID, read again as SPIRE rotates it. SPIRE renews an SVID at
//! half its lifetime and writes the new certificate and key over the old ones, so a
//! processor that read them once would sign events, and serve the brake's control API,
//! with an expired SVID until it restarted: the collector logs such events invalid.
//!
//! The files are checked every RELOAD_EVERY. A new set is used only once it's whole: the
//! key must be the certificate's, and the trust bundle, when there is one, must parse. A
//! write SPIRE is half way through, or a broken file, keeps the last good set, and is tried
//! again on the next check.

use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::Duration;

use sha2::{Digest, Sha256};

use crate::signer::Signer;

/// How often the SVID's files are checked for a new one.
pub const RELOAD_EVERY: Duration = Duration::from_secs(5);

/// One whole set: the signer, and the PEM it came from, for the control API's TLS.
pub struct Material {
    pub signer: Signer,
    pub cert_pem: String,
    pub key_pem: String,
    /// The trust bundle callers' SVIDs chain to, when one is read.
    pub bundle_pem: Option<String>,
    /// What the files held, so a check that finds them unchanged does nothing.
    digest: [u8; 32],
}

impl Material {
    /// Identifies this set: a new one means a new TLS configuration.
    pub fn digest(&self) -> [u8; 32] {
        self.digest
    }
}

struct Files {
    cert: PathBuf,
    key: PathBuf,
    bundle: Option<PathBuf>,
}

/// The SVID in use, and where it's read from.
pub struct Svid {
    files: Option<Files>,
    current: RwLock<Arc<Material>>,
}

impl From<Signer> for Arc<Svid> {
    /// A signer that never changes, with no TLS material.
    fn from(signer: Signer) -> Self {
        Arc::new(Svid {
            files: None,
            current: RwLock::new(Arc::new(Material {
                signer,
                cert_pem: String::new(),
                key_pem: String::new(),
                bundle_pem: None,
                digest: [0; 32],
            })),
        })
    }
}

impl Svid {
    /// Reads the SVID's certificate chain and key, and the trust bundle when given. Fails
    /// when they don't make a whole set, since there's no last good one to keep yet.
    pub fn from_files(cert: &Path, key: &Path, bundle: Option<&Path>) -> Result<Self, String> {
        let files = Files {
            cert: cert.to_path_buf(),
            key: key.to_path_buf(),
            bundle: bundle.map(Path::to_path_buf),
        };
        let material = read(&files)?;
        Ok(Svid {
            files: Some(files),
            current: RwLock::new(Arc::new(material)),
        })
    }

    /// The set in use now.
    pub fn current(&self) -> Arc<Material> {
        self.current.read().unwrap_or_else(|e| e.into_inner()).clone()
    }

    /// Reads the files again, and uses what they hold if it changed and is whole. Returns
    /// whether it changed; an error keeps the set in use.
    pub fn reload(&self) -> Result<bool, String> {
        let Some(files) = &self.files else {
            return Ok(false);
        };
        let digest = digest_of(files)?;
        if digest == self.current().digest {
            return Ok(false);
        }
        let material = read(files)?;
        tracing::info!(signer = material.signer.id(), "read a new SVID");
        *self.current.write().unwrap_or_else(|e| e.into_inner()) = Arc::new(material);
        Ok(true)
    }

    /// Checks for a new SVID every RELOAD_EVERY until shutdown resolves.
    pub async fn watch(self: Arc<Self>, shutdown: impl std::future::Future<Output = ()>) {
        if self.files.is_none() {
            return;
        }
        tokio::pin!(shutdown);
        let mut tick = tokio::time::interval(RELOAD_EVERY);
        loop {
            tokio::select! {
                _ = &mut shutdown => return,
                _ = tick.tick() => {
                    if let Err(error) = self.reload() {
                        tracing::warn!(error, "can't use the SVID's files yet; keeping the one in use");
                    }
                }
            }
        }
    }
}

fn read_file(path: &Path) -> Result<String, String> {
    std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))
}

fn digest_of(files: &Files) -> Result<[u8; 32], String> {
    let mut h = Sha256::new();
    for path in [Some(&files.cert), Some(&files.key), files.bundle.as_ref()]
        .into_iter()
        .flatten()
    {
        let bytes = std::fs::read(path).map_err(|e| format!("{}: {e}", path.display()))?;
        h.update((bytes.len() as u64).to_be_bytes());
        h.update(&bytes);
    }
    Ok(h.finalize().into())
}

fn read(files: &Files) -> Result<Material, String> {
    let cert_pem = read_file(&files.cert)?;
    let key_pem = read_file(&files.key)?;
    let bundle_pem = files.bundle.as_deref().map(read_file).transpose()?;
    let signer = Signer::from_pem(&key_pem, &cert_pem).map_err(|e| format!("{}: {e}", files.cert.display()))?;
    if let Some(bundle) = &bundle_pem
        && !pem::parse_many(bundle).is_ok_and(|certs| !certs.is_empty())
    {
        return Err(format!(
            "{}: no certificates",
            files.bundle.as_deref().unwrap_or(Path::new("")).display()
        ));
    }
    // The digest covers exactly what was parsed, so a check right after sees no change.
    let mut h = Sha256::new();
    for text in [Some(&cert_pem), Some(&key_pem), bundle_pem.as_ref()]
        .into_iter()
        .flatten()
    {
        h.update((text.len() as u64).to_be_bytes());
        h.update(text.as_bytes());
    }
    Ok(Material {
        signer,
        cert_pem,
        key_pem,
        bundle_pem,
        digest: h.finalize().into(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::signer::dev_identity;

    struct Dir(PathBuf);

    impl Drop for Dir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn dir() -> Dir {
        let d = std::env::temp_dir().join(format!("wake-svid-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&d).unwrap();
        Dir(d)
    }

    fn write(d: &Path, cert: &str, key: &str, bundle: &str) {
        std::fs::write(d.join("proxy.pem"), cert).unwrap();
        std::fs::write(d.join("proxy.key"), key).unwrap();
        std::fs::write(d.join("bundle.pem"), bundle).unwrap();
    }

    fn open(d: &Path) -> Result<Svid, String> {
        Svid::from_files(&d.join("proxy.pem"), &d.join("proxy.key"), Some(&d.join("bundle.pem")))
    }

    #[test]
    fn uses_a_rotated_svid_once_its_files_are_whole() {
        let d = dir();
        let first = dev_identity("spiffe://wake.local/proxy").unwrap();
        write(&d.0, &first.cert_pem, &first.key_pem, &first.ca_pem);
        let svid = open(&d.0).unwrap();
        assert_eq!(svid.current().cert_pem, first.cert_pem);
        assert_eq!(svid.reload(), Ok(false), "unchanged");

        // SPIRE writes the new certificate first: until the key follows, the pair isn't
        // whole, so the old one stays in use.
        let second = dev_identity("spiffe://wake.local/proxy").unwrap();
        std::fs::write(d.0.join("proxy.pem"), &second.cert_pem).unwrap();
        let err = svid.reload().unwrap_err();
        assert!(err.contains("isn't the certificate's"), "{err}");
        assert_eq!(svid.current().cert_pem, first.cert_pem);

        std::fs::write(d.0.join("proxy.key"), &second.key_pem).unwrap();
        std::fs::write(d.0.join("bundle.pem"), &second.ca_pem).unwrap();
        assert_eq!(svid.reload(), Ok(true));
        let now = svid.current();
        assert_eq!(now.cert_pem, second.cert_pem);
        assert_eq!(now.bundle_pem.as_deref(), Some(second.ca_pem.as_str()));
        assert_eq!(now.signer.id(), "spiffe://wake.local/proxy");

        // A broken or missing file keeps the set in use too.
        std::fs::write(d.0.join("bundle.pem"), "not a bundle").unwrap();
        assert!(svid.reload().is_err());
        std::fs::remove_file(d.0.join("proxy.key")).unwrap();
        assert!(svid.reload().is_err());
        assert_eq!(svid.current().cert_pem, second.cert_pem);
    }

    #[test]
    fn refuses_to_start_without_a_whole_set() {
        let d = dir();
        let a = dev_identity("spiffe://wake.local/proxy").unwrap();
        let b = dev_identity("spiffe://wake.local/proxy").unwrap();
        write(&d.0, &a.cert_pem, &b.key_pem, &a.ca_pem);
        assert!(open(&d.0).is_err());
        write(&d.0, &a.cert_pem, &a.key_pem, "");
        assert!(open(&d.0).is_err());
        write(&d.0, &a.cert_pem, &a.key_pem, &a.ca_pem);
        assert!(open(&d.0).is_ok());
    }

    #[test]
    fn signs_with_the_svid_in_use() {
        let d = dir();
        let first = dev_identity("spiffe://wake.local/proxy").unwrap();
        write(&d.0, &first.cert_pem, &first.key_pem, &first.ca_pem);
        let svid = open(&d.0).unwrap();
        let certificate = |svid: &Svid| {
            let mut e = crate::canonical::Event::default();
            svid.current().signer.sign(&mut e);
            e.str("wake.signature.certificate").unwrap().to_string()
        };
        let before = certificate(&svid);
        let second = dev_identity("spiffe://wake.local/proxy").unwrap();
        write(&d.0, &second.cert_pem, &second.key_pem, &second.ca_pem);
        svid.reload().unwrap();
        assert_ne!(certificate(&svid), before, "events carry the new SVID");
    }
}
