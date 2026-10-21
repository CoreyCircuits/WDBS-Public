# Security

- Do not open public issues containing credentials, tokens, hostnames or logs from a real deployment.
- Report security problems privately to the maintainer (contact to be added before release).
- The code is designed so that: the Stream server binds to loopback only; key relays must authenticate and allowlist
  keys; tokens are read from environment or mode-600 files and never printed; tests never contact live accounts.
- Optional integrations (calendar, task database, ledger, Stremio) use credentials supplied by the operator. Treat
  `env.example` files as templates only.
