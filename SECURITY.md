# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: on the repository page,
**Security → Report a vulnerability**. Do not open a public issue for
anything that could be exploited.

This is a hobby project maintained in spare time, so there is no guaranteed
response time, but reports are taken seriously.

## Scope and threat model

LAN Walkie is designed for a **trusted network**. It has no user accounts: anyone who can
reach the port can join the channel. That is by design, not a vulnerability.
In scope: crashes or resource exhaustion triggered by a network peer, ways to
bypass the priority token, leaks of the token or TLS key, and anything that
lets a peer harm another participant's machine (terminal injection, etc.).

Supported version: the latest commit on `main`.
