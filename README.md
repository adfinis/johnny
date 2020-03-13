johnny - generic dep(p)endencies tracker
========================================

Tracking versions and alerting stale dependencies. Johnny is tool and language
independent and doesn't need central service.

Currently supports: git, gitlab (gitlab.com and self-hosted), github, arch and aur

For git please prefer git:// is supported, but please prefer https://

Note!: Most git URLs end in ".git" (github/gitlab), but API URLs do not.

Running
-------

```bash
pip install johnny
johnny examples/deps.toml
```
