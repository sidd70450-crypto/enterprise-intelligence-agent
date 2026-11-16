# Information security standard SEC-07

## Encryption

All customer data is encrypted at rest with AES-256. Encryption keys are held in the cloud key management service and rotated every 90 days. Data in transit must use TLS 1.2 or later; TLS 1.0 and 1.1 are blocked at the load balancer.

## Access control

Production access requires hardware security keys; one-time codes sent by text message are not accepted. Access is granted for at most eight hours at a time through the just-in-time access tool and every session is recorded.

## Vulnerability management

Critical vulnerabilities must be patched within 7 days and high-severity ones within 30 days. An external firm performs a penetration test every year, and the results are reported to the audit committee.
