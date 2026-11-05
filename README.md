# The Ultimate Streaming Control Panel for Linux

A Stream Deck daemon and web control panel for streaming from Linux with OBS and Twitch.

| Tool | What it is |
|---|---|
| [`ultimate-streaming/`](ultimate-streaming/) | **The Ultimate Streaming Control Panel for Linux**: Stream Deck daemon (every Elgato model) for OBS, Twitch and your desktop, plus a builder for a matching OBS profile and scene set with an interactive BRB screen |

Every tool is self-contained in its own folder, with its own README listing prerequisites
and install steps. Nothing here contains credentials, channel names or machine-specific
paths: you supply those in your own config, which stays outside the repository.

See [CHANGELOG.md](CHANGELOG.md) for what changed in each release and the known issues, and
[SECURITY.md](SECURITY.md) for supported versions and how to report a vulnerability.

## Contributing

Before your first commit, turn on the privacy check that ships with the repo:

```sh
git config core.hooksPath .githooks
```

It runs on every commit and refuses one that would publish credentials, config files,
email addresses or absolute home paths. To also block your own personal words (channel
name, client id, hostnames, device serials), list them one per line in
`.git/info/private-patterns`. That file is inside `.git/`, so it is never committed.
`.githooks/pre-commit --all` checks every tracked file.

## License

Copyright 2026 Zachary Fouts. Licensed under the [Apache License 2.0](LICENSE).

If you redistribute this project or a modified version of it, include the [NOTICE](NOTICE)
file with it, as the license requires. Third-party packages are downloaded at install time,
not included here, and keep their own licenses (see each tool's README).
