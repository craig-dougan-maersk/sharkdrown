"""Bundled resources for the Claude UI module.

Currently contains:

- ``Dockerfile.claude-code`` — used by ``docker_image_builder`` to
  build the per-host-UID container image at runtime. Bundled here
  (via ``importlib.resources.files``) so the build works without
  a repo checkout.

Sync the canonical repo-root copy at ``Dockerfile.claude-code``
when editing the bundled file. Both must stay byte-identical.
"""