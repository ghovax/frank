This session may not {{ action }} `{{ path }}`.

The path lies outside the confinement this session runs under, which the operating system enforces for shell commands and the harness applies to its own file tools, so the two agree.

Your context lists what the confinement permits. Work inside it, or re-issue the call with an `access_request` naming the path you need and why.
