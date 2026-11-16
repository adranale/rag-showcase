# Security Policy

## Supported Versions

Only the latest release receives security fixes (see the
[releases page](https://github.com/adranale/rag-showcase/releases)).

## Reporting a Vulnerability

Please do not open a public issue. Report vulnerabilities privately via
[GitHub private vulnerability reporting](https://github.com/adranale/rag-showcase/security/advisories/new).
Include the affected version, steps to reproduce and the impact. Never include
real credentials or personal data in a report.

## Multi-user data protection (v2)

Each user's documents are isolated by PostgreSQL row-level security and `user_id` filters; Dropbox refresh tokens are encrypted at rest. The service operator (database administrator / app owner) can still technically access all data; this is disclosed to users. Work/school Microsoft accounts are not supported yet. See "Multi-user mode (v2)" in the README for details and limitations.
