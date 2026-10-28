"""Research harness import for the shared, finite DeepSeek V4.1 encoder.

Runtime dispatch uses the frozen identity wrapper in the agent runtime. The
calibration helpers may also request the research-only thinking template.
"""

from packages.agent_runtime.deepseek_v41_counter import (
    MAX_MESSAGE_BYTES as MAX_MESSAGE_BYTES,
)
from packages.agent_runtime.deepseek_v41_counter import (
    MAX_TOKENIZER_BYTES as MAX_TOKENIZER_BYTES,
)
from packages.agent_runtime.deepseek_v41_counter import (
    MODEL as MODEL,
)
from packages.agent_runtime.deepseek_v41_counter import (
    TOKENIZER_REVISION as TOKENIZER_REVISION,
)
from packages.agent_runtime.deepseek_v41_counter import (
    TOKENIZER_SHA256 as TOKENIZER_SHA256,
)
from packages.agent_runtime.deepseek_v41_counter import (
    DeepseekV41TextCounter as DeepseekV41TextCounter,
)
