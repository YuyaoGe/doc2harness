# Evaluation image

stage2 runs every scenario that needs isolation inside one image, built from
`containers/Dockerfile`: a Python runtime with the pinned dependencies in
`requirements/runtime.lock`, an empty `/opt/f0`, and no OpenCode.

The image contains no product. The caller injects the artifact under test at
run time, so one image serves every product.

The stage1 builder image is a different image; see
[`src/stage1/README.md`](../src/stage1/README.md).

## Building

```sh
docker build --target evaluation \
  --tag doc2harness/evaluation:candidate \
  --file containers/Dockerfile .
```

That tag is the default the code looks for. Override it with
`SBH_EVALUATION_IMAGE` if you tag differently.

## Mirrors

The checked-in defaults use Aliyun's Debian and PyPI mirrors. They are build
arguments, not edits to the Dockerfile:

```sh
docker build --target evaluation \
  --build-arg DEBIAN_MIRROR=https://deb.debian.org/debian \
  --build-arg DEBIAN_SECURITY_MIRROR=https://security.debian.org/debian-security \
  --build-arg PYPI_INDEX_URL=https://pypi.org/simple \
  --tag doc2harness/evaluation:candidate \
  --file containers/Dockerfile .
```

Changing a mirror must not change what lands in the image. The Python base is
pinned by digest and the Python dependencies by `requirements/runtime.lock`; a
mirror serves those same packages or the build fails.

## What the build asserts

The build ends with checks that fail it rather than ship a wrong image:
OpenCode is absent, `/opt/f0` is empty, and no mini-swe-agent tree,
distribution, or cache is present.
