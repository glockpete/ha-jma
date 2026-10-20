# Security policy

## Reporting a vulnerability

Please report a security problem privately through GitHub: **Security**, **Report a vulnerability**
(<https://github.com/glockpete/ha-jma/security/advisories/new>). Do not open a public issue for it.

You can expect a first answer within a week. The integration reads public data files and accepts no inbound
connection; the most useful reports are about a way a crafted upstream file could crash Home Assistant or leak the
home location (the diagnostics file and the request URLs are the places that carry it).

## Supported versions

Only the latest release receives fixes.
