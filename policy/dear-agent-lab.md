---
project: dear-agent-lab
repos:
  - "dearagent-dev/dear-agent-lab-*"
enabled: true
base_branch: main
harness: opencode
verify_allow:
  - python -m unittest
  - make test
---

# Dear Agent lab projects

Policy for the `dearagent-dev/dear-agent-lab-*` practice repositories.

The front matter above is the editable source of truth: run
`dear-agent policy sync --dir policy` to project it into the database, where the worker reads
it. `verify_allow` **adds** to the operator's global `DEAR_AGENT_VERIFY_ALLOW` allowlist for
these repos; it can never widen beyond running an allowlisted argv.
