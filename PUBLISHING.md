# Publishing rcpilot to PyPI

This guide covers how to publish rcpilot to PyPI.

## Prerequisites

1. **PyPI Account**: Create accounts on both [PyPI](https://pypi.org) and [TestPyPI](https://test.pypi.org)
2. **API Tokens**: Generate API tokens for both PyPI and TestPyPI
   - PyPI: https://pypi.org/manage/account/token/
   - TestPyPI: https://test.pypi.org/manage/account/token/
3. **uv installed**: The project uses `uv` for building

## Version Management

Before publishing, update the version in `pyproject.toml`:

```toml
[project]
version = "0.4.3"  # Update this
```

Follow [semantic versioning](https://semver.org/):
- **MAJOR**: Breaking changes
- **MINOR**: New features, backwards compatible
- **PATCH**: Bug fixes, backwards compatible

## Building the Package

Build both source distribution and wheel:

```bash
uv build
```

This creates:
- `dist/rcpilot-{version}.tar.gz` (source distribution)
- `dist/rcpilot-{version}-py3-none-any.whl` (wheel)

Verify the contents:

```bash
# Check source distribution
tar -tzf dist/rcpilot-*.tar.gz

# Check wheel
unzip -l dist/rcpilot-*.whl
```

## Publishing to TestPyPI (Recommended First)

Test your package on TestPyPI before publishing to the real PyPI:

```bash
# Install twine if not already installed
uv tool install twine

# Upload to TestPyPI
uvx twine upload --repository testpypi dist/*
```

When prompted, use:
- Username: `__token__`
- Password: Your TestPyPI API token (starts with `pypi-`)

Test installation from TestPyPI:

```bash
# Install from TestPyPI
uvx --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ rcpilot

# Or with uv
uv tool install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ rcpilot
```

Note: The `--extra-index-url` is needed because dependencies are on the real PyPI.

## Publishing to PyPI

Once you've verified everything works on TestPyPI:

```bash
uvx twine upload dist/*
```

When prompted, use:
- Username: `__token__`
- Password: Your PyPI API token (starts with `pypi-`)

## Verify the Release

After publishing:

1. Check the package page: https://pypi.org/project/rcpilot/
2. Test installation:
   ```bash
   uvx rcpilot --help
   # or
   uv tool install rcpilot
   pilot --help
   ```

## Using PyPI Tokens with Environment Variables

For automation, store tokens as environment variables:

```bash
# Add to ~/.bashrc or ~/.zshrc
export TWINE_USERNAME=__token__
export TWINE_PASSWORD=pypi-...  # Your PyPI token
```

Then upload without prompts:

```bash
uvx twine upload dist/*
```

## GitHub Release (Optional)

After publishing to PyPI, create a GitHub release:

1. Tag the version:
   ```bash
   git tag -a v0.4.3 -m "Release v0.4.3"
   git push origin v0.4.3
   ```

2. Create release on GitHub:
   - Go to https://github.com/kjozsa/rcpilot/releases/new
   - Select the tag
   - Add release notes
   - Attach the built distributions (optional)

## Troubleshooting

### Build Failures

If `uv build` fails:
- Check `pyproject.toml` syntax
- Ensure all classifiers are valid (see https://pypi.org/classifiers/)
- Verify all files referenced exist

### Upload Failures

- **Version already exists**: You cannot overwrite a version on PyPI. Increment the version number.
- **Invalid token**: Regenerate your API token
- **File too large**: PyPI has a 100MB limit per file

### Installation Issues

- **Missing dependencies**: Ensure all dependencies are listed in `pyproject.toml`
- **Static files missing**: Check `[tool.hatch.build.targets.wheel]` configuration

## Checklist

Before publishing:

- [ ] Update version in `pyproject.toml`
- [ ] Update `CHANGELOG.md` (if exists)
- [ ] Run tests: `uv run pytest`
- [ ] Build package: `uv build`
- [ ] Verify package contents
- [ ] Test on TestPyPI
- [ ] Publish to PyPI
- [ ] Create Git tag
- [ ] Create GitHub release
- [ ] Test installation: `uvx rcpilot`
