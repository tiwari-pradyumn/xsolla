# Python + FastAPI as the service stack

The mock rules and test fixtures are JS-flavored, which superficially suggests Node/TS, but the language scanning a diff is independent of the language inside it. We chose Python + FastAPI because the author must defend every line in an interview and has production experience with this stack and none with Node. Known cost: the MOCK-002 regex from the brief is JS syntax and must be ported to Python `re` with `re.IGNORECASE`, pinned by shared fixtures.
