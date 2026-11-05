# Security policy

## Supported versions

Only the latest 0.2.x release receives security fixes.

## Reporting a vulnerability

Please report privately, not in a public issue: open the repository's **Security** tab on
GitHub and choose **Report a vulnerability** (a private security advisory). Include what you
found, the steps to reproduce it and the version (`python3 main.py --version`). Please give
a reasonable time to fix it before disclosing.

## Threat model

- The control panel is a local service bound to 127.0.0.1 and protected by an access token;
  it is not meant to be exposed to a network.
- Plugins run with your full user rights. Install only plugins you trust.
- Configs imported from other deck apps are reviewed key by key before anything in them runs.
