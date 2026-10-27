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
    /// Loads a PKCS#8 P-256 private key and its certificate chain, both PEM.
    pub fn from_pem(key_pem: &str, cert_pem: &str) -> Result<Self, String> {
        let key = SigningKey::from_pkcs8_pem(key_pem).map_err(|e| format!("signing key: {e}"))?;
        // The SVID first, then any intermediates, as SPIRE writes them.
        let chain = pem::parse_many(cert_pem).map_err(|e| format!("certificate: {e}"))?;
        let leaf = chain.first().ok_or("no certificate")?;
        let id = spiffe_id(leaf.contents())?;
        let der: Vec<u8> = chain.iter().flat_map(|c| c.contents().to_vec()).collect();
        Ok(Signer {
            key,
            id,
            certificate: STANDARD.encode(der),
        })
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
    /// A TLS server certificate and key signed by the same CA, for the collector's
    /// receiver, when TLS hosts were asked for.
    pub tls: Option<(String, String)>,
    /// An SVID and key for the brake, which the control API checks, when asked for.
    pub brake: Option<(String, String)>,
    /// A TLS server certificate and key for agentgateway, which it serves agents mTLS with,
    /// when gateway hosts were asked for.
    pub gateway: Option<(String, String)>,
    /// Each agent asked for, with an SVID and key: spiffe://<trust domain>/agent/<name>.
    pub agents: Vec<(String, String, String)>,
}

/// What else a development identity issues, besides the processor's SVID.
#[derive(Default)]
pub struct DevExtras<'a> {
    /// Host names for the collector's TLS server certificate.
    pub tls_hosts: &'a [String],
    /// The brake's SPIFFE ID, for its SVID.
    pub brake_id: Option<&'a str>,
    /// Host names for agentgateway's TLS server certificate.
    pub gateway_hosts: &'a [String],
    /// Agents to issue SVIDs, by name.
    pub agents: &'a [String],
}

pub fn dev_identity(spiffe_id: &str) -> Result<DevIdentity, String> {
    dev_identity_with(spiffe_id, &DevExtras::default())
}

/// A development identity, plus a TLS server certificate for these hosts when there are
/// any, and an SVID for the brake when its SPIFFE ID is given.
pub fn dev_identity_with_tls(
    spiffe_id: &str,
    tls_hosts: &[String],
    brake_id: Option<&str>,
) -> Result<DevIdentity, String> {
    dev_identity_with(
        spiffe_id,
        &DevExtras {
            tls_hosts,
            brake_id,
            ..Default::default()
        },
    )
}

/// The trust domain of a SPIFFE ID: spiffe://<trust domain>/<path>.
pub fn trust_domain(spiffe_id: &str) -> Option<&str> {
    let rest = spiffe_id.strip_prefix("spiffe://")?;
    let td = rest.split('/').next()?;
    (!td.is_empty()).then_some(td)
}

/// Whether a name can be an agent's: a SPIFFE ID path segment and a file name.
pub fn valid_agent_name(name: &str) -> bool {
    (1..=64).contains(&name.len())
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
        && !name.starts_with('.')
}

/// A development identity, plus whatever extras asks for, all signed by one CA.
pub fn dev_identity_with(spiffe_id: &str, extras: &DevExtras) -> Result<DevIdentity, String> {
    let DevExtras {
        tls_hosts,
        brake_id,
        gateway_hosts,
        agents,
    } = *extras;
    use rcgen::{
        BasicConstraints, CertificateParams, DnType, ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair, KeyUsagePurpose,
        PKCS_ECDSA_P256_SHA256, SanType,
    };
    let err = |e: rcgen::Error| e.to_string();

    // Strict X.509 checks, which Python 3.13 makes the default, want a CA's key usage and
    // each issued certificate's authority key identifier.
    let ca_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).map_err(err)?;
    let mut ca = CertificateParams::new(Vec::<String>::new()).map_err(err)?;
    ca.distinguished_name
        .push(DnType::CommonName, "Wake development trust bundle");
    ca.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    ca.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    let ca_cert = ca.self_signed(&ca_key).map_err(err)?;

    let key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).map_err(err)?;
    let mut leaf = CertificateParams::new(Vec::<String>::new()).map_err(err)?;
    leaf.subject_alt_names = vec![SanType::URI(spiffe_id.try_into().map_err(err)?)];
    // An X.509-SVID signs; it never issues certificates (SPIFFE X509-SVID, section 4.3).
    leaf.key_usages = vec![KeyUsagePurpose::DigitalSignature];
    leaf.use_authority_key_identifier_extension = true;
    let issuer = Issuer::new(ca, ca_key);
    let cert = leaf.signed_by(&key, &issuer).map_err(err)?;

    let server = |hosts: &[String]| -> Result<(String, String), String> {
        let server_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).map_err(err)?;
        let mut server = CertificateParams::new(hosts.to_vec()).map_err(err)?;
        server.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        server.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
        server.use_authority_key_identifier_extension = true;
        let server_cert = server.signed_by(&server_key, &issuer).map_err(err)?;
        Ok((server_cert.pem(), server_key.serialize_pem()))
    };
    let client = |id: &str| -> Result<(String, String), String> {
        let client_key = KeyPair::generate_for(&PKCS_ECDSA_P256_SHA256).map_err(err)?;
        let mut client = CertificateParams::new(Vec::<String>::new()).map_err(err)?;
        client.subject_alt_names = vec![SanType::URI(id.try_into().map_err(err)?)];
        client.key_usages = vec![KeyUsagePurpose::DigitalSignature];
        client.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
        client.use_authority_key_identifier_extension = true;
        let client_cert = client.signed_by(&client_key, &issuer).map_err(err)?;
        Ok((client_cert.pem(), client_key.serialize_pem()))
    };

    let tls = (!tls_hosts.is_empty()).then(|| server(tls_hosts)).transpose()?;
    let brake = brake_id.map(client).transpose()?;
    let gateway = (!gateway_hosts.is_empty()).then(|| server(gateway_hosts)).transpose()?;
    let agents = agents
        .iter()
        .map(|name| {
            let td = trust_domain(spiffe_id).ok_or_else(|| format!("{spiffe_id} isn't a SPIFFE ID"))?;
            if !valid_agent_name(name) {
                return Err(format!("{name:?} isn't an agent name: 1 to 64 of A-Z a-z 0-9 . _ -"));
            }
            let (cert, key) = client(&format!("spiffe://{td}/agent/{name}"))?;
            Ok((name.clone(), cert, key))
        })
        .collect::<Result<_, String>>()?;

    Ok(DevIdentity {
        ca_pem: ca_cert.pem(),
        cert_pem: cert.pem(),
        key_pem: key.serialize_pem(),
        tls,
        brake,
        gateway,
        agents,
    })
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
        let der = STANDARD
            .decode(event.str("wake.signature.certificate").unwrap())
            .unwrap();
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
    fn issues_the_gateway_and_each_agent_from_the_same_ca() {
        let id = dev_identity_with(
            "spiffe://wake.local/proxy",
            &DevExtras {
                gateway_hosts: &["agentgateway".into(), "localhost".into()],
                agents: &["billing".into(), "research".into()],
                ..Default::default()
            },
        )
        .unwrap();
        assert!(id.gateway.is_some());
        let ids: Vec<String> = id
            .agents
            .iter()
            .map(|(_, cert, _)| spiffe_id(pem::parse(cert).unwrap().contents()).unwrap())
            .collect();
        assert_eq!(
            ids,
            [
                "spiffe://wake.local/agent/billing",
                "spiffe://wake.local/agent/research"
            ]
        );
        for bad in ["", "../x", "a b", ".hidden"] {
            let extras = DevExtras {
                agents: &[bad.to_string()],
                ..Default::default()
            };
            assert!(
                dev_identity_with("spiffe://wake.local/proxy", &extras).is_err(),
                "{bad:?}"
            );
        }
        assert_eq!(trust_domain("spiffe://example.org/agent/x"), Some("example.org"));
        assert_eq!(trust_domain("https://example.org"), None);
    }

    #[test]
    fn refuses_certificates_without_one_spiffe_id() {
        let id = dev_identity("spiffe://wake.local/proxy").unwrap();
        // The CA certificate has no SAN at all.
        assert!(Signer::from_pem(&id.key_pem, &id.ca_pem).is_err());
        assert!(
            dev_identity("https://not-spiffe")
                .map(|i| Signer::from_pem(&i.key_pem, &i.cert_pem))
                .unwrap()
                .is_err()
        );
    }
}
