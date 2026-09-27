#!/bin/bash
# Build the profiling PyBoy (2.7.0 + per-PC cycle histogram + immediate button events)
# into tools/bench/.pyboy. Needs python3-dev, gcc, network.
set -e
HERE=$(cd "$(dirname "$0")" && pwd); D="$HERE/.pyboy"; mkdir -p "$D"; cd "$D"
[ -d venv ] || python3 -m venv --system-site-packages venv
venv/bin/pip install -q "cython>=3.0.6,<3.1,!=3.0.10" setuptools numpy pillow
[ -f pyboy-2.7.0.tar.gz ] || curl -sLO https://files.pythonhosted.org/packages/7b/90/60ab95f484d1d792488ea30852b0820c0527b0f79fa555c57f214028c7b7/pyboy-2.7.0.tar.gz
rm -rf pyboy-2.7.0 && tar xzf pyboy-2.7.0.tar.gz && cd pyboy-2.7.0
patch -p0 < "$HERE/pyboy-2.7.0-prof.patch"
../venv/bin/python setup.py build_ext --inplace > build.log 2>&1
echo "ok: run tools/bench/run.sh"
