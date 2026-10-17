"""plate_daemon.llm: the Anthropic composer and the two-sample consensus judge (task T33).

Everything here is custody-gated and fixture-tested: `AnthropicClient` refuses a call the run's allowed grades
do not cover (zero transport calls), the default transport replays recordings, and the live transport exists
only under PLATE_LIVE=1 with a budget envelope. Import the submodules directly; this package root stays light.
"""
