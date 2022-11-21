# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2021-2022 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This module contains the APY estimation ABCI application."""

from packages.valory.skills.abstract_round_abci.abci_app_chain import (
    AbciAppTransitionMapping,
    chain,
)
from packages.valory.skills.apy_estimation_abci.rounds import (
    APYEstimationAbciApp,
    FailedAPYRound,
    FinishedAPYEstimationRound,
    ModelStrategyRound,
)
from packages.valory.skills.registration_abci.rounds import (
    AgentRegistrationAbciApp,
    FinishedRegistrationFFWRound,
    FinishedRegistrationRound,
    RegistrationRound,
)
from packages.valory.skills.reset_pause_abci.rounds import (
    FinishedResetAndPauseErrorRound,
    FinishedResetAndPauseRound,
    ResetAndPauseRound,
    ResetPauseAbciApp,
)


abci_app_transition_mapping: AbciAppTransitionMapping = {
    FinishedRegistrationRound: ModelStrategyRound,
    FinishedRegistrationFFWRound: ModelStrategyRound,
    FinishedAPYEstimationRound: ResetAndPauseRound,
    FailedAPYRound: ResetAndPauseRound,
    FinishedResetAndPauseRound: ModelStrategyRound,
    FinishedResetAndPauseErrorRound: RegistrationRound,
}

APYEstimationAbciAppChained = chain(
    (
        AgentRegistrationAbciApp,
        APYEstimationAbciApp,
        ResetPauseAbciApp,
    ),
    abci_app_transition_mapping,
)
