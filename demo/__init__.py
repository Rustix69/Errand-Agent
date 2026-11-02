"""Terminal demo for the errand agent. Lives OUTSIDE ``errand/`` on purpose: it drives the simulator hooks of the
mock world (provider cancels, forced timeouts ...), and nothing under ``errand/`` may import the mocks."""
