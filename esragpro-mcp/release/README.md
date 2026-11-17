# Release Configuration (WP8)

This directory contains release scripts and configuration for esRAGPro MCP Gateway.

## Release Checklist

### Pre-Release
- [ ] All tests pass (`cargo test --all-features`)
- [ ] No security vulnerabilities (`cargo audit`)
- [ ] Documentation updated
- [ ] Version bumped in Cargo.toml
- [ ] CHANGELOG.md updated

### Build
- [ ] Release build (`cargo build --release`)
- [ ] SBOM generated (`cargo sbom`)
- [ ] Binary size optimized

### Smoke Tests
- [ ] stdio mode: exactly 6 tools (reader profile)
- [ ] HTTP mode + management-profile: exactly 10 tools (admin profile)
- [ ] All management tools require correct scope
- [ ] ACL leakage tests pass (count = 0)

### Deployment
- [ ] Container image built and tagged
- [ ] Helm chart updated (if applicable)
- [ ] Backup verified
- [ ] Rollback procedure tested

## Release Artifacts

| Artifact | Description |
|----------|-------------|
| `esragpro-mcp-{version}-{target}.tar.gz` | Binary distribution |
| `sbom.json` | Software Bill of Materials |
| `checksums.sha256` | SHA256 checksums |

## Version Scheme

- Major: Breaking API changes
- Minor: New features, backward compatible
- Patch: Bug fixes, backward compatible
- Pre-release: `-alpha`, `-beta`, `-rc.N`

## Rollback Procedure

1. Stop the service
2. Deploy previous version binary
3. Restart the service
4. Verify health check
5. Run smoke tests