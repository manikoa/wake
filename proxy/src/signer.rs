//! Signs events with the processor's own identity: an ECDSA P-256 key and an X.509
//! certificate whose URI SAN is the processor's SPIFFE ID, as SPIRE issues them. See
//! docs/proxy.md. agentgateway never sees the key.

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use p256::ecdsa::{DerSignature, SigningKey, signature::Signer as _};
use p256::pkcs8::DecodePrivateKey;
use sha2::{Digest, Sha256};

use crate::canonical::{Event, Value, canonical};

pub struct Signer {
    key: SigningKey,
    id: String,
    certificate: String,
}

impl Signer {
    /// Loads a PKCS#8 P-256 private key and its certificate, both PEM.
    pub fn from_pem(key_pem: &str, cert_pem: &str) -> Result<Self, String> {
        let key = SigningKey::from_pkcs8_pem(key_pem).map_err(|e| format!("signing key: {e}"))?;
        let cert = pem::parse(cert_pem).map_err(|e| format!("certificate: {e}"))?;
        let id = spiffe_id(cert.contents())?;
        Ok(Signer { key, id, certificate: STANDARD.encode(cert.contents()) })
    }

    /// The SPIFFE ID this signer signs as.
    pub fn id(&self) -> &str {
        &self.id
    }

    /// Signs an event. It sets the payload's digest first, from the payload itself, so the
    /// signature commits to exactly what was captured. The certificate travels with the
    /// event so anyone holding the trust bundle can check it later.
    pub fn sign(&self, event: &mut Event) {
        if let Some(Value::Str(payload)) = event.attributes.get("wake.payload") {
            let digest = hex::encode(Sha256::digest(payload.as_bytes()));
            event.set("wake.payload.sha256", Value::Str(digest));
        }
        let signature: DerSignature = self.key.sign(&canonical(event));
        event.set("wake.signature", Value::Str(STANDARD.encode(signature.as_bytes())));
        event.set("wake.signer", Value::Str(self.id.clone()));
        event.set("wake.signature.certificate", Value::Str(self.certificate.clone()));
    }
}

/// The SPIFFE ID in a certificate: its one spiffe:// URI SAN.
pub fn spiffe_id(der: &[u8]) -> Result<String, String> {
    use x509_parser::prelude::*;
    let (_, cert) = X509Certificate::from_der(der).map_err(|e| format!("certificate: {e}"))?;
    let san = cert
        .subject_alternative_name()
        .map_err(|e| format!("certificate SAN: {e}"))?
        .ok_or("certificate has no SAN")?;
    let ids: Vec<&str> = san
        .value
        .general_names
        .iter()
        .filter_map(|n| match n {
            GeneralName::URI(uri) if uri.starts_with("spiffe://") => Some(*uri),
            _ => None,
        })
        .collect();
    match ids.as_slice() {
        [id] => Ok(id.to_string()),
        [] => Err("certificate has no spiffe:// URI SAN".into()),
        _ => Err("certificate has more than one spiffe:// URI SAN".into()),
    }
}

/// A development identity: a CA standing in for SPIRE's trust bundle, and a P-256 key and
/// certificate for the processor, signed by it. Never for production, where SPIRE issues
/// short lived certificates.
pub struct DevIdentity {
    pub ca_pem: String,
    pub cert_pem: String,
    pub key_pem: String,
}

pub fn dev_identity(spiffe_id: &str) -> Result<DevIdentity, String> {
    use rcgen::{BasicConstraints, CertificateParams, DnType, IsCa, Issuer, KeyPair, PKCS_ECDSA_P256_SHA256, SanType};
    let err = |e: rcgen::Error| e.to_string();

    let ca_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).map_err(err)?;
    let mut ca = CertificateParams::new(Vec::<String>::new()).map_err(err)?;
    ca.distinguished_name.push(DnType::CommonName, "Wake development trust bundle");
    ca.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    let ca_cert = ca.self_signed(&ca_key).map_err(err)?;

    let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).map_err(err)?;
    let mut leaf = CertificateParams::new(Vec::<String>::new()).map_err(err)?;
    leaf.subject_alt_names = vec![SanType::URI(spiffe_id.try_into().map_err(err)?)];
    let issuer = Issuer::new(ca, ca_key);
    let cert = leaf.signed_by(&key, &issuer).map_err(err)?;

    Ok(DevIdentity { ca_pem: ca_cert.pem(), cert_pem: cert.pem(), key_pem: key.serialize_pem() })
}

#[cfg(test)]
mod tests {
    use super::*;
    use p256::ecdsa::{Signature, VerifyingKey, signature::Verifier};

    fn signer() -> Signer {
        let id = dev_identity("spiffe://wake.local/proxy").unwrap();
        Signer::from_pem(&id.key_pem, &id.cert_pem).unwrap()
    }

    #[test]
    fn signs_canonical_bytes_with_the_certificates_key() {
        let s = signer();
        assert_eq!(s.id(), "spiffe://wake.local/proxy");
        let mut event = Event::default();
        event.set("wake.event.id", Value::Str("e1".into()));
        event.set("wake.payload", Value::Str(r#"{"output":"wire $10,000"}"#.into()));
        s.sign(&mut event);

        assert_eq!(
            event.str("wake.payload.sha256"),
            Some(hex::encode(Sha256::digest(br#"{"output":"wire $10,000"}"#)).as_str())
        );
        assert_eq!(event.str("wake.signer"), Some("spiffe://wake.local/proxy"));

        // Check the way a verifier would: the key from the certificate the event carries.
        let der = STANDARD.decode(event.str("wake.signature.certificate").unwrap()).unwrap();
        let (_, cert) = x509_parser::parse_x509_certificate(&der).unwrap();
        let key = VerifyingKey::from_sec1_bytes(cert.public_key().subject_public_key.as_ref()).unwrap();
        let sig = Signature::from_der(&STANDARD.decode(event.str("wake.signature").unwrap()).unwrap()).unwrap();
        key.verify(&canonical(&event), &sig).unwrap();

        // The payload isn't in the signed bytes, but its digest is.
        let mut tampered = event.clone();
        tampered.set("wake.event.id", Value::Str("e2".into()));
        assert!(key.verify(&canonical(&tampered), &sig).is_err());
        let mut other_payload = event.clone();
        other_payload.set("wake.payload", Value::Str("{}".into()));
        key.verify(&canonical(&other_payload), &sig).unwrap();
    }

    #[test]
    fn refuses_certificates_without_one_spiffe_id() {
        let id = dev_identity("spiffe://wake.local/proxy").unwrap();
        // The CA certificate has no SAN at all.
        assert!(Signer::from_pem(&id.key_pem, &id.ca_pem).is_err());
        assert!(dev_identity("https://not-spiffe").map(|i| Signer::from_pem(&i.key_pem, &i.cert_pem)).unwrap().is_err());
    }
}
