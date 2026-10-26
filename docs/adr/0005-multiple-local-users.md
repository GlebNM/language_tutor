# Separate accounts and learning stores in one local installation

On 22 October 2026 the user requested multiple users and approved username/password registration. This supersedes ADR 0002. Each account owns one learning profile and a separate directory containing its SQLite database, AI settings and subscription credentials. Server sessions select the directory; authenticated internal MCP requests carry the same ownership. This preserves existing learning transactions and isolates caches and background jobs without adding owner predicates to every existing query. Cross-user reporting and shared materials would require a separate design.

The first account explicitly adopts the existing local profile, with a SQLite backup and retained source files. Subsequent accounts receive independent stores. Delivery stays on loopback through Docker Compose. A single OAuth callback port serializes new subscription connections, while connected users can run learning requests concurrently.
