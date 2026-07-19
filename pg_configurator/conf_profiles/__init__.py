from pg_configurator.conf_profiles.ext_perf import ext_alg_set as ext_alg_set
from pg_configurator.conf_profiles.profile_1c import alg_set_1c as alg_set_1c
from pg_configurator.conf_profiles.profile_backend_common import (
    backend_common_alg_set as backend_common_alg_set,
)
from pg_configurator.conf_profiles.profile_backend_perf import (
    backend_perf_alg_set as backend_perf_alg_set,
)

__all__ = [
    "alg_set_1c",
    "backend_common_alg_set",
    "backend_perf_alg_set",
    "ext_alg_set",
]
