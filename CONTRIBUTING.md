# Contributing

Thank you for your help.

1. Open an issue first for a large change. Say what you want to change and why.
2. Fork the repo and branch from `develop`.
3. Read `CLAUDE.md`. It lists the rules and how to test a change.
4. Test with a scratch workspace and a dummy printer URL. Never test against a printer with a part on the bed.
5. Add an entry to `CHANGELOG.md`.
6. Open a pull request against `develop`. Describe what you tested.

Style:

- Python: stdlib only in `tools/` and `lib/workspace.py`; Python 3.10 must run them.
- JavaScript: plain ES modules, no build step.
- Docs and comments: short, simple sentences, active voice.
- No personal data in the repo: no names, LAN addresses, hostnames or user files.

By contributing, you agree that your work is released under the MIT License.
