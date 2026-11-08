"""Command-line interface for the Declarative Agent SDK.

Deliberately does not re-export ``main`` from :mod:`.main`: binding that name
here would shadow the submodule, so an attribute chain like
``declarative_agent_sdk.cli.main.main()`` — which is how PyApp's
``PYAPP_EXEC_SPEC`` invokes the entry point — would resolve ``.main`` to the
function and then fail on ``.main`` again.  Import from the submodule directly:

    from declarative_agent_sdk.cli.main import main
"""
