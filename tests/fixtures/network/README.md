The loopback certificate and private key are public, synthetic test fixtures.
They were generated solely for the local HTTPS proxy tests using the existing
Windows .NET cryptography API. They identify only `localhost` and `127.0.0.1`,
expire in 2040, and are not application or provider credentials.

Tests explicitly trust this certificate in their own TLS context. Production
client construction must still request certificate verification. The fixture
key must never be used for any real service and is not part of the portable
application's runtime data.
