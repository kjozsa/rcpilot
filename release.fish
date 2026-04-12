#!/usr/bin/env fish

rm -rf dist/ && uv build
uvx twine upload dist/*

