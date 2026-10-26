Status: superseded by [ADR 0005](0005-multiple-local-users.md).

# One local learner without application accounts

Each local installation serves one learner, and the standalone web app and Chrome extension use the same learning data. The user removed application authentication, multiple learner profiles, and profile switching from MVP on 19 October 2026, so the data model has no application users, login sessions, extension credentials, or pairing codes. Authorizing the learner's AI subscription grants model access and remains separate from the learning profile.
