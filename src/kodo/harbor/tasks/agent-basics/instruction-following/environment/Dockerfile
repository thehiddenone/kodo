# agent-basics/instruction-following (doc/AGENT_BUILDER.md §5.4). Pinned by tag
# and digest so every run scores the same workspace on the same interpreter.
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

# The fixture builder doubles as the snapshot tool each step's setup.sh runs.
COPY make_fixture.py /opt/agent-basics/make_fixture.py
RUN python3 /opt/agent-basics/make_fixture.py build /app \
    && mkdir -p /var/lib/agent-basics

WORKDIR /app
