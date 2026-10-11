"""stage1: build pipeline where a model + opencode produce a harness artifact.

Entry point: run_build.py.
Artifact acceptance gates live in smoke/ (docker cp'd into the container at
build time); specs/contracts in spec/ and contract/.

Standard library only, like the stage2 evaluation core: building a product needs
Python 3.11+ and Docker and nothing else.
"""
