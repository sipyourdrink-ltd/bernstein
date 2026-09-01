## Session revocation emits a chain event with bounded propagation

Session revocation now emits a chain event that records the revocation with bounded
propagation, allowing auditors to verify when each enforcement point stopped
honouring the revoked session. (#5031)
