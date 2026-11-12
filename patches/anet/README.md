# ANet reliability and maintenance changes

Upstream: https://github.com/ANetResearch/ANet

Base commit: `6dc9b6793e46d3a630c9836349e5ca635d4ef761`

Local implementation commit: `9d1c98e1c10d17da3913047fb8c827ce121794fc`

`execution-reliability.patch` contains the changes between these revisions.
It includes managed workspaces, execution metadata, attachment reliability,
signed operator maintenance, idle handoff and rollback. All affected files
are modified relative to the named base; added files are indicated by the patch.
The local implementation commit is a provenance identifier, not a promise that
it exists on the upstream remote.

## Apply to a separate checkout

```sh
git clone https://github.com/ANetResearch/ANet.git
cd ANet
git checkout 6dc9b6793e46d3a630c9836349e5ca635d4ef761
git switch -c agent-space-reliability
git apply --check ../agent-space/patches/anet/execution-reliability.patch
git apply ../agent-space/patches/anet/execution-reliability.patch
```

ANet requires the Go version named in its `go.mod`. The integration tests in
Agent Space use a fake transport and do not compile or validate this patch.

## License

The complete [ANet license](LICENSE) applies to this patch and the resulting
derivative work. Preserve upstream copyright, attribution and applicable UI
marks. This material is **not** covered by the root Apache-2.0 license.
The license includes restrictions on multi-tenant hosted hubs; redistributing
source does not grant permission to operate such a service outside its terms.
No production identity, private signing key, live database or task data is included.
