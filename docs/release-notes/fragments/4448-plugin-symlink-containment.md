## A plugin's symlinks cannot reach outside the pack

Installing an Agent Plugins directory containment-checked the manifest's
`skills` field but followed whatever the entries under it pointed at, so a
skill directory, a bucket or a nested file symlinked out of the pack was
copied into the install scope -- content the operator never saw in the tree
they inspected. Every walked path now has to resolve inside the pack: an
escaping skill directory is skipped and reported, an escaping bucket or file
refuses the skill's install.
