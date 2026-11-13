# Security policy

WinPC Remote lets a remote model control a real Windows PC, so we take security issues seriously.

## How to report a vulnerability

**Please do not disclose vulnerability details in a public Issue.**

- Prefer GitHub's *Security -> Report a vulnerability* (private vulnerability reporting);
- Please include in the report: the affected version (`agent_version` from `get_status`), reproduction steps, and the impact you see (for example, "in prompt mode X can be read without confirmation").

**Please do not** attach real tokens, private keys or private screen content to reports, screenshots or logs; reproduce with fake data.

## Scope

We care about: bypassing token authentication or the Host / Origin checks; bypassing application or file authorization (block-lists, sensitive paths, `file_roots`, extension blocking, symlinks/UNC/alternate data streams, etc.); reading or modifying the token, private keys or the Agent's own directory through the file tools or the transfer tools (`get_file` / `put_file`), or bypassing authorization; the emergency stop being lifted remotely; the audit log leaking content that should not be recorded.

The following are **known design limitations**, not vulnerabilities; see "Security model and known risks" in the README:

- `approval_mode: all` / `bypass` removes the confirmation dialogs;
- application authorization is not a sandbox: an approved application (a terminal, the Run box of the file manager, etc.) can launch arbitrary programs;
- prompt injection through screen content can only be mitigated, not eliminated;
- other malicious programs under the same Windows account can read the plain-text token;
- the sensitive-path block-list cannot cover the credential locations of every piece of software.

## Supported versions

Only the latest release is fixed. Before upgrading, read the "Changelog" section of the README.
