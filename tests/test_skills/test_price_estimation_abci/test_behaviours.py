# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2021 Valory AG
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

"""Tests for valory/price_estimation_abci skill's behaviours."""
import json
import logging
import time
from copy import copy
from pathlib import Path
from typing import Any, Dict, Type, cast
from unittest import mock
from unittest.mock import patch

import pytest
from aea.exceptions import AEAActException
from aea.helpers.transaction.base import (
    RawTransaction,
    SignedMessage,
    SignedTransaction,
)
from aea.helpers.transaction.base import State as TrState
from aea.helpers.transaction.base import TransactionDigest, TransactionReceipt
from aea.test_tools.test_skill import BaseSkillTestCase

from packages.open_aea.protocols.signing import SigningMessage
from packages.valory.connections.http_client.connection import (
    PUBLIC_ID as HTTP_CLIENT_PUBLIC_ID,
)
from packages.valory.connections.ledger.base import (
    CONNECTION_ID as LEDGER_CONNECTION_PUBLIC_ID,
)
from packages.valory.contracts.gnosis_safe.contract import (
    PUBLIC_ID as GNOSIS_SAFE_CONTRACT_ID,
)
from packages.valory.protocols.abci import AbciMessage  # noqa: F401
from packages.valory.protocols.contract_api.message import ContractApiMessage
from packages.valory.protocols.http import HttpMessage
from packages.valory.protocols.ledger_api.message import LedgerApiMessage
from packages.valory.skills.abstract_round_abci.base import (
    AbstractRound,
    BasePeriodState,
    BaseTxPayload,
    OK_CODE,
    _MetaPayload,
)
from packages.valory.skills.abstract_round_abci.behaviour_utils import BaseState
from packages.valory.skills.abstract_round_abci.behaviours import AbstractRoundBehaviour
from packages.valory.skills.price_estimation_abci.behaviours import (
    DeploySafeBehaviour,
    EstimateBehaviour,
    FinalizeBehaviour,
    ObserveBehaviour,
    PriceEstimationConsensusBehaviour,
    RandomnessAtStartupBehaviour,
    RandomnessInOperationBehaviour,
    RegistrationBehaviour,
    ResetBehaviour,
    SelectKeeperABehaviour,
    SelectKeeperAtStartupBehaviour,
    SelectKeeperBBehaviour,
    SignatureBehaviour,
    TendermintHealthcheckBehaviour,
    TransactionHashBehaviour,
    ValidateSafeBehaviour,
    ValidateTransactionBehaviour,
)
from packages.valory.skills.price_estimation_abci.handlers import (
    ContractApiHandler,
    HttpHandler,
    LedgerApiHandler,
    SigningHandler,
)
from packages.valory.skills.price_estimation_abci.rounds import Event, PeriodState

from tests.conftest import ROOT_DIR


class DummyRoundId:
    """Dummy class for setting round_id for exit condition."""

    round_id: str

    def __init__(self, round_id: str) -> None:
        """Dummy class for setting round_id for exit condition."""
        self.round_id = round_id


class PriceEstimationFSMBehaviourBaseCase(BaseSkillTestCase):
    """Base case for testing PriceEstimation FSMBehaviour."""

    path_to_skill = Path(
        ROOT_DIR, "packages", "valory", "skills", "price_estimation_abci"
    )

    price_estimation_behaviour: PriceEstimationConsensusBehaviour
    ledger_handler: LedgerApiHandler
    http_handler: HttpHandler
    contract_handler: ContractApiHandler
    signing_handler: SigningHandler
    old_tx_type_to_payload_cls: Dict[str, Type[BaseTxPayload]]

    @classmethod
    def setup(cls, **kwargs: Any) -> None:
        """Setup the test class."""
        # we need to store the current value of the meta-class attribute
        # _MetaPayload.transaction_type_to_payload_cls, and restore it
        # in the teardown function. We do a shallow copy so we avoid
        # to modify the old mapping during the execution of the tests.
        cls.old_tx_type_to_payload_cls = copy(
            _MetaPayload.transaction_type_to_payload_cls
        )
        _MetaPayload.transaction_type_to_payload_cls = {}
        super().setup()
        assert cls._skill.skill_context._agent_context is not None
        cls._skill.skill_context._agent_context.identity._default_address_key = (
            "ethereum"
        )
        cls._skill.skill_context._agent_context._default_ledger_id = "ethereum"
        cls.price_estimation_behaviour = cast(
            PriceEstimationConsensusBehaviour,
            cls._skill.skill_context.behaviours.main,
        )
        cls.http_handler = cast(HttpHandler, cls._skill.skill_context.handlers.http)
        cls.signing_handler = cast(
            SigningHandler, cls._skill.skill_context.handlers.signing
        )
        cls.contract_handler = cast(
            ContractApiHandler, cls._skill.skill_context.handlers.contract_api
        )
        cls.ledger_handler = cast(
            LedgerApiHandler, cls._skill.skill_context.handlers.ledger_api
        )

        cls.price_estimation_behaviour.setup()
        cls._skill.skill_context.state.setup()
        assert (
            cast(BaseState, cls.price_estimation_behaviour.current_state).state_id
            == cls.price_estimation_behaviour.initial_state_cls.state_id
        )

    def fast_forward_to_state(
        self,
        behaviour: AbstractRoundBehaviour,
        state_id: str,
        period_state: BasePeriodState,
    ) -> None:
        """Fast forward the FSM to a state."""
        next_state = {s.state_id: s for s in behaviour.behaviour_states}[state_id]
        assert next_state is not None, f"State {state_id} not found"
        next_state = cast(Type[BaseState], next_state)
        behaviour.current_state = next_state(
            name=next_state.state_id, skill_context=behaviour.context
        )
        self.skill.skill_context.state.period.abci_app._round_results.append(
            period_state
        )
        if next_state.matching_round is not None:
            self.skill.skill_context.state.period.abci_app._current_round = (
                next_state.matching_round(
                    period_state, self.skill.skill_context.params.consensus_params
                )
            )

    def mock_ledger_api_request(
        self, request_kwargs: Dict, response_kwargs: Dict
    ) -> None:
        """
        Mock http request.

        :param request_kwargs: keyword arguments for request check.
        :param response_kwargs: keyword arguments for mock response.
        """

        self.assert_quantity_in_outbox(1)
        actual_ledger_api_message = self.get_message_from_outbox()
        assert actual_ledger_api_message is not None, "No message in outbox."
        has_attributes, error_str = self.message_has_attributes(
            actual_message=actual_ledger_api_message,
            message_type=LedgerApiMessage,
            to=str(LEDGER_CONNECTION_PUBLIC_ID),
            sender=str(self.skill.skill_context.skill_id),
            **request_kwargs,
        )

        assert has_attributes, error_str
        incoming_message = self.build_incoming_message(
            message_type=LedgerApiMessage,
            dialogue_reference=(
                actual_ledger_api_message.dialogue_reference[0],
                "stub",
            ),
            target=actual_ledger_api_message.message_id,
            message_id=-1,
            to=str(self.skill.skill_context.skill_id),
            sender=str(LEDGER_CONNECTION_PUBLIC_ID),
            ledger_id=str(LEDGER_CONNECTION_PUBLIC_ID),
            **response_kwargs,
        )
        self.ledger_handler.handle(incoming_message)
        self.price_estimation_behaviour.act_wrapper()

    def mock_contract_api_request(
        self, request_kwargs: Dict, response_kwargs: Dict
    ) -> None:
        """
        Mock http request.

        :param request_kwargs: keyword arguments for request check.
        :param response_kwargs: keyword arguments for mock response.
        """

        self.assert_quantity_in_outbox(1)
        actual_contract_ledger_message = self.get_message_from_outbox()
        assert actual_contract_ledger_message is not None, "No message in outbox."
        has_attributes, error_str = self.message_has_attributes(
            actual_message=actual_contract_ledger_message,
            message_type=ContractApiMessage,
            to=str(LEDGER_CONNECTION_PUBLIC_ID),
            sender=str(self.skill.skill_context.skill_id),
            ledger_id="ethereum",
            contract_id=str(GNOSIS_SAFE_CONTRACT_ID),
            message_id=1,
            **request_kwargs,
        )
        assert has_attributes, error_str
        self.price_estimation_behaviour.act_wrapper()

        incoming_message = self.build_incoming_message(
            message_type=ContractApiMessage,
            dialogue_reference=(
                actual_contract_ledger_message.dialogue_reference[0],
                "stub",
            ),
            target=actual_contract_ledger_message.message_id,
            message_id=-1,
            to=str(self.skill.skill_context.skill_id),
            sender=str(LEDGER_CONNECTION_PUBLIC_ID),
            ledger_id="ethereum",
            contract_id=str(GNOSIS_SAFE_CONTRACT_ID),
            **response_kwargs,
        )
        self.contract_handler.handle(incoming_message)
        self.price_estimation_behaviour.act_wrapper()

    def mock_http_request(self, request_kwargs: Dict, response_kwargs: Dict) -> None:
        """
        Mock http request.

        :param request_kwargs: keyword arguments for request check.
        :param response_kwargs: keyword arguments for mock response.
        """

        self.assert_quantity_in_outbox(1)
        actual_http_message = self.get_message_from_outbox()
        assert actual_http_message is not None, "No message in outbox."
        has_attributes, error_str = self.message_has_attributes(
            actual_message=actual_http_message,
            message_type=HttpMessage,
            performative=HttpMessage.Performative.REQUEST,
            to=str(HTTP_CLIENT_PUBLIC_ID),
            sender=str(self.skill.skill_context.skill_id),
            **request_kwargs,
        )
        assert has_attributes, error_str
        self.price_estimation_behaviour.act_wrapper()
        self.assert_quantity_in_outbox(0)
        incoming_message = self.build_incoming_message(
            message_type=HttpMessage,
            dialogue_reference=(actual_http_message.dialogue_reference[0], "stub"),
            performative=HttpMessage.Performative.RESPONSE,
            target=actual_http_message.message_id,
            message_id=-1,
            to=str(self.skill.skill_context.skill_id),
            sender=str(HTTP_CLIENT_PUBLIC_ID),
            **response_kwargs,
        )
        self.http_handler.handle(incoming_message)
        self.price_estimation_behaviour.act_wrapper()

    def mock_signing_request(self, request_kwargs: Dict, response_kwargs: Dict) -> None:
        """Mock signing request."""
        self.assert_quantity_in_decision_making_queue(1)
        actual_signing_message = self.get_message_from_decision_maker_inbox()
        assert actual_signing_message is not None, "No message in outbox."
        has_attributes, error_str = self.message_has_attributes(
            actual_message=actual_signing_message,
            message_type=SigningMessage,
            to="dummy_decision_maker_address",
            sender=str(self.skill.skill_context.skill_id),
            **request_kwargs,
        )
        assert has_attributes, error_str
        incoming_message = self.build_incoming_message(
            message_type=SigningMessage,
            dialogue_reference=(actual_signing_message.dialogue_reference[0], "stub"),
            target=actual_signing_message.message_id,
            message_id=-1,
            to=str(self.skill.skill_context.skill_id),
            sender="dummy_decision_maker_address",
            **response_kwargs,
        )
        self.signing_handler.handle(incoming_message)
        self.price_estimation_behaviour.act_wrapper()

    def mock_a2a_transaction(
        self,
    ) -> None:
        """Performs mock a2a transaction."""

        self.mock_signing_request(
            request_kwargs=dict(
                performative=SigningMessage.Performative.SIGN_MESSAGE,
            ),
            response_kwargs=dict(
                performative=SigningMessage.Performative.SIGNED_MESSAGE,
                signed_message=SignedMessage(
                    ledger_id="ethereum", body="stub_signature"
                ),
            ),
        )

        self.mock_http_request(
            request_kwargs=dict(
                method="GET",
                headers="",
                version="",
                body=b"",
            ),
            response_kwargs=dict(
                version="",
                status_code=200,
                status_text="",
                headers="",
                body=json.dumps({"result": {"deliver_tx": {"code": OK_CODE}}}).encode(
                    "utf-8"
                ),
            ),
        )

    def end_round(
        self,
    ) -> None:
        """Ends round early to cover `wait_for_end` generator."""
        current_state = cast(BaseState, self.price_estimation_behaviour.current_state)
        if current_state is None:
            return
        current_state = cast(BaseState, current_state)
        if current_state.matching_round is None:
            return
        abci_app = current_state.context.state.period.abci_app
        old_round = abci_app._current_round
        abci_app._last_round = old_round
        abci_app._current_round = abci_app.transition_function[
            current_state.matching_round
        ][Event.DONE](abci_app.state, abci_app.consensus_params)
        self.price_estimation_behaviour._process_current_round()

    def _test_done_flag_set(self) -> None:
        """Test that, when round ends, the 'done' flag is set."""
        current_state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert not current_state.is_done()
        with mock.patch.object(
            self.price_estimation_behaviour.context.state, "period"
        ) as mock_period:
            mock_period.last_round_id = cast(
                AbstractRound, current_state.matching_round
            ).round_id
            current_state.act_wrapper()
            assert current_state.is_done()

    @classmethod
    def teardown(cls) -> None:
        """Teardown the test class."""
        _MetaPayload.transaction_type_to_payload_cls = cls.old_tx_type_to_payload_cls  # type: ignore


class TestTendermintHealthcheckBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test case to test TendermintHealthcheckBehaviour."""

    def test_tendermint_healthcheck_not_live(self) -> None:
        """Test the tendermint health check does not finish if not healthy."""
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == TendermintHealthcheckBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()

        with patch.object(
            self.price_estimation_behaviour.context.logger, "log"
        ) as mock_logger:
            self.mock_http_request(
                request_kwargs=dict(
                    method="GET",
                    url=self.skill.skill_context.params.tendermint_url + "/health",
                    headers="",
                    version="",
                    body=b"",
                ),
                response_kwargs=dict(
                    version="",
                    status_code=500,
                    status_text="",
                    headers="",
                    body=b"",
                ),
            )
        mock_logger.assert_any_call(
            logging.ERROR, "Tendermint not running, trying again!"
        )
        time.sleep(1)
        self.price_estimation_behaviour.act_wrapper()

    def test_tendermint_healthcheck_not_live_raises(self) -> None:
        """Test the tendermint health check raises if not healthy for too long."""
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == TendermintHealthcheckBehaviour.state_id
        )
        self.skill.skill_context.params._count_healthcheck = (
            self.skill.skill_context.params._max_healthcheck + 1
        )
        with pytest.raises(AEAActException, match="Tendermint node did not come live!"):
            self.price_estimation_behaviour.act_wrapper()

    def test_tendermint_healthcheck_live(self) -> None:
        """Test the tendermint health check does finish if healthy."""
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == TendermintHealthcheckBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        with patch.object(
            self.price_estimation_behaviour.context.logger, "log"
        ) as mock_logger:
            self.mock_http_request(
                request_kwargs=dict(
                    method="GET",
                    url=self.skill.skill_context.params.tendermint_url + "/health",
                    headers="",
                    version="",
                    body=b"",
                ),
                response_kwargs=dict(
                    version="",
                    status_code=200,
                    status_text="",
                    headers="",
                    body=json.dumps({}).encode("utf-8"),
                ),
            )

        mock_logger.assert_any_call(logging.INFO, "Tendermint running.")
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == RegistrationBehaviour.state_id


class TestRegistrationBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test case to test RegistrationBehaviour."""

    def test_registration(self) -> None:
        """Test registration."""
        self.fast_forward_to_state(
            self.price_estimation_behaviour,
            RegistrationBehaviour.state_id,
            PeriodState(),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == RegistrationBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_a2a_transaction()

        # for sender in ["sender_a", "sender_b", "sender_c", "sender_d"]:  # noqa: E800
        #     incoming_message = self.build_incoming_message(  # noqa: E800
        #         message_type=AbciMessage,  # noqa: E800
        #         dialogue_reference=("stub", ""),  # noqa: E800
        #         performative=AbciMessage.Performative.REQUEST_DELIVER_TX,  # noqa: E800
        #         target=0,  # noqa: E800
        #         message_id=1,  # noqa: E800
        #         to=str(self.skill.skill_context.skill_id),  # noqa: E800
        #         sender=str(ABCI_SERVER_PUBLIC_ID),  # noqa: E800
        #         tx=,  # noqa: E800
        #     )  # noqa: E800
        #     self.http_handler.handle(incoming_message)  # noqa: E800
        # self.price_estimation_behaviour.act_wrapper()  # noqa: E800

        self._test_done_flag_set()

        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == RandomnessAtStartupBehaviour.state_id


class BaseRandomnessBehaviourTest(PriceEstimationFSMBehaviourBaseCase):
    """Test RandomnessBehaviour."""

    randomness_behaviour_class: Type[BaseState]
    next_behaviour_class: Type[BaseState]

    def test_randomness_behaviour(
        self,
    ) -> None:
        """Test RandomnessBehaviour."""

        self.fast_forward_to_state(
            self.price_estimation_behaviour,
            self.randomness_behaviour_class.state_id,
            PeriodState(),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == self.randomness_behaviour_class.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_http_request(
            request_kwargs=dict(
                method="GET",
                headers="",
                version="",
                body=b"",
                url="https://drand.cloudflare.com/public/latest",
            ),
            response_kwargs=dict(
                version="",
                status_code=200,
                status_text="",
                headers="",
                body=json.dumps(
                    {
                        "round": 1283255,
                        "randomness": "04d4866c26e03347d2431caa82ab2d7b7bdbec8b58bca9460c96f5265d878feb",
                    }
                ).encode("utf-8"),
            ),
        )

        self.price_estimation_behaviour.act_wrapper()
        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()

        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == self.next_behaviour_class.state_id

    def test_invalid_response(
        self,
    ) -> None:
        """Test invalid json response."""
        self.fast_forward_to_state(
            self.price_estimation_behaviour,
            self.randomness_behaviour_class.state_id,
            PeriodState(),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == self.randomness_behaviour_class.state_id
        )
        self.price_estimation_behaviour.act_wrapper()

        self.mock_http_request(
            request_kwargs=dict(
                method="GET",
                headers="",
                version="",
                body=b"",
                url="https://drand.cloudflare.com/public/latest",
            ),
            response_kwargs=dict(
                version="", status_code=200, status_text="", headers="", body=b""
            ),
        )
        self.price_estimation_behaviour.act_wrapper()
        time.sleep(1)
        self.price_estimation_behaviour.act_wrapper()

    def test_max_retries_reached(
        self,
    ) -> None:
        """Test with max retries reached."""
        self.fast_forward_to_state(
            self.price_estimation_behaviour,
            self.randomness_behaviour_class.state_id,
            PeriodState(),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == self.randomness_behaviour_class.state_id
        )
        with mock.patch.object(
            self.price_estimation_behaviour.context.randomness_api,
            "is_retries_exceeded",
            return_value=True,
        ):
            self.price_estimation_behaviour.act_wrapper()
            state = cast(BaseState, self.price_estimation_behaviour.current_state)
            assert state.state_id == self.randomness_behaviour_class.state_id
            self._test_done_flag_set()


class TestRandomnessAtStartup(BaseRandomnessBehaviourTest):
    """Test randomness at startup."""

    randomness_behaviour_class = RandomnessAtStartupBehaviour
    next_behaviour_class = SelectKeeperAtStartupBehaviour


class TestRandomnessInOperation(BaseRandomnessBehaviourTest):
    """Test randomness in operation."""

    randomness_behaviour_class = RandomnessInOperationBehaviour
    next_behaviour_class = SelectKeeperABehaviour


class BaseSelectKeeperBehaviourTest(PriceEstimationFSMBehaviourBaseCase):
    """Test SelectKeeperBehaviour."""

    select_keeper_behaviour_class: Type[BaseState]
    next_behaviour_class: Type[BaseState]

    def test_select_keeper(
        self,
    ) -> None:
        """Test select keeper agent."""
        participants = frozenset({self.skill.skill_context.agent_address, "a_1", "a_2"})
        self.fast_forward_to_state(
            behaviour=self.price_estimation_behaviour,
            state_id=self.select_keeper_behaviour_class.state_id,
            period_state=PeriodState(
                participants,
                most_voted_randomness="56cbde9e9bbcbdcaf92f183c678eaa5288581f06b1c9c7f884ce911776727688",
            ),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == self.select_keeper_behaviour_class.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == self.next_behaviour_class.state_id


class TestSelectKeeperStartupBehaviour(BaseSelectKeeperBehaviourTest):
    """Test SelectKeeperBehaviour."""

    select_keeper_behaviour_class = SelectKeeperAtStartupBehaviour
    next_behaviour_class = DeploySafeBehaviour


class TestSelectKeeperABehaviour(BaseSelectKeeperBehaviourTest):
    """Test SelectKeeperBehaviour."""

    select_keeper_behaviour_class = SelectKeeperABehaviour
    next_behaviour_class = ObserveBehaviour


class TestSelectKeeperBBehaviour(BaseSelectKeeperBehaviourTest):
    """Test SelectKeeperBehaviour."""

    select_keeper_behaviour_class = SelectKeeperBBehaviour
    next_behaviour_class = FinalizeBehaviour


class TestDeploySafeBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test DeploySafeBehaviour."""

    def test_deployer_act(
        self,
    ) -> None:
        """Run tests."""
        participants = frozenset({self.skill.skill_context.agent_address, "a_1", "a_2"})
        most_voted_keeper_address = self.skill.skill_context.agent_address
        self.fast_forward_to_state(
            self.price_estimation_behaviour,
            DeploySafeBehaviour.state_id,
            PeriodState(
                participants=participants,
                most_voted_keeper_address=most_voted_keeper_address,
                safe_contract_address="safe_contract_address",
            ),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == DeploySafeBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()

        self.mock_contract_api_request(
            request_kwargs=dict(
                performative=ContractApiMessage.Performative.GET_DEPLOY_TRANSACTION,
            ),
            response_kwargs=dict(
                performative=ContractApiMessage.Performative.RAW_TRANSACTION,
                callable="get_deploy_transaction",
                raw_transaction=RawTransaction(
                    ledger_id="ethereum",
                    body={"contract_address": "safe_contract_address"},
                ),
            ),
        )

        self.mock_signing_request(
            request_kwargs=dict(
                performative=SigningMessage.Performative.SIGN_TRANSACTION,
            ),
            response_kwargs=dict(
                performative=SigningMessage.Performative.SIGNED_TRANSACTION,
                signed_transaction=SignedTransaction(ledger_id="ethereum", body={}),
            ),
        )

        self.mock_ledger_api_request(
            request_kwargs=dict(
                performative=LedgerApiMessage.Performative.SEND_SIGNED_TRANSACTION,
            ),
            response_kwargs=dict(
                performative=LedgerApiMessage.Performative.TRANSACTION_DIGEST,
                transaction_digest=TransactionDigest(
                    ledger_id="ethereum", body="tx_hash"
                ),
            ),
        )

        self.mock_ledger_api_request(
            request_kwargs=dict(
                performative=LedgerApiMessage.Performative.GET_TRANSACTION_RECEIPT,
            ),
            response_kwargs=dict(
                performative=LedgerApiMessage.Performative.TRANSACTION_RECEIPT,
                transaction_receipt=TransactionReceipt(
                    ledger_id="ethereum", receipt={}, transaction={}
                ),
            ),
        )

        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == ValidateSafeBehaviour.state_id

    def test_not_deployer_act(
        self,
    ) -> None:
        """Run tests."""
        participants = frozenset({self.skill.skill_context.agent_address, "a_1", "a_2"})
        most_voted_keeper_address = "a_1"
        self.fast_forward_to_state(
            self.price_estimation_behaviour,
            DeploySafeBehaviour.state_id,
            PeriodState(
                participants=participants,
                most_voted_keeper_address=most_voted_keeper_address,
                safe_contract_address="safe_contract_address",
            ),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == DeploySafeBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self._test_done_flag_set()
        self.end_round()
        time.sleep(1)
        self.price_estimation_behaviour.act_wrapper()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == ValidateSafeBehaviour.state_id


class TestValidateSafeBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test ValidateSafeBehaviour."""

    def test_validate_safe_behaviour(self) -> None:
        """Run test."""
        self.fast_forward_to_state(
            self.price_estimation_behaviour,
            ValidateSafeBehaviour.state_id,
            PeriodState(safe_contract_address="safe_contract_address"),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == ValidateSafeBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_contract_api_request(
            request_kwargs=dict(
                performative=ContractApiMessage.Performative.GET_STATE,
            ),
            response_kwargs=dict(
                performative=ContractApiMessage.Performative.STATE,
                callable="verify_contract",
                state=TrState(ledger_id="ethereum", body={"verified": True}),
            ),
        )

        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == RandomnessInOperationBehaviour.state_id


class TestObserveBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test ObserveBehaviour."""

    def test_observer_behaviour(
        self,
    ) -> None:
        """Run tests."""
        self.fast_forward_to_state(
            self.price_estimation_behaviour,
            ObserveBehaviour.state_id,
            PeriodState(estimate=1.0),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == ObserveBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_http_request(
            request_kwargs=dict(
                method="GET",
                url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
                headers="",
                version="",
                body=b"",
            ),
            response_kwargs=dict(
                version="",
                status_code=200,
                status_text="",
                headers="",
                body=json.dumps({"bitcoin": {"usd": 54566}}).encode("utf-8"),
            ),
        )
        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == EstimateBehaviour.state_id

    def test_observer_behaviour_retries_exceeded(
        self,
    ) -> None:
        """Run tests."""
        self.fast_forward_to_state(
            self.price_estimation_behaviour,
            ObserveBehaviour.state_id,
            PeriodState(estimate=1.0),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == ObserveBehaviour.state_id
        )
        with mock.patch.object(
            self.price_estimation_behaviour.context.price_api,
            "is_retries_exceeded",
            return_value=True,
        ):
            self.price_estimation_behaviour.act_wrapper()
            state = cast(BaseState, self.price_estimation_behaviour.current_state)
            assert state.state_id == ObserveBehaviour.state_id
            self._test_done_flag_set()

    def test_observed_value_none(
        self,
    ) -> None:
        """Test when `observed` value is none."""
        self.fast_forward_to_state(
            self.price_estimation_behaviour, ObserveBehaviour.state_id, PeriodState()
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == ObserveBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_http_request(
            request_kwargs=dict(
                method="GET",
                url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
                headers="",
                version="",
                body=b"",
            ),
            response_kwargs=dict(
                version="",
                status_code=200,
                status_text="",
                headers="",
                body=b"",
            ),
        )
        time.sleep(1)
        self.price_estimation_behaviour.act_wrapper()


class TestEstimateBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test EstimateBehaviour."""

    def test_estimate(
        self,
    ) -> None:
        """Test estimate behaviour."""

        self.fast_forward_to_state(
            behaviour=self.price_estimation_behaviour,
            state_id=EstimateBehaviour.state_id,
            period_state=PeriodState(estimate=1.0),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == EstimateBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == TransactionHashBehaviour.state_id


class TestTransactionHashBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test TransactionHashBehaviour."""

    def test_estimate(
        self,
    ) -> None:
        """Test estimate behaviour."""

        self.fast_forward_to_state(
            behaviour=self.price_estimation_behaviour,
            state_id=TransactionHashBehaviour.state_id,
            period_state=PeriodState(
                most_voted_estimate=1.0,
                safe_contract_address="safe_contract_address",
                most_voted_keeper_address="most_voted_keeper_address",
            ),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == TransactionHashBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_contract_api_request(
            request_kwargs=dict(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,
            ),
            response_kwargs=dict(
                performative=ContractApiMessage.Performative.RAW_TRANSACTION,
                callable="get_deploy_transaction",
                raw_transaction=RawTransaction(
                    ledger_id="ethereum", body={"tx_hash": "0x3b"}
                ),
            ),
        )
        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == SignatureBehaviour.state_id


class TestSignatureBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test SignatureBehaviour."""

    def test_signature_behaviour(
        self,
    ) -> None:
        """Test signature behaviour."""

        self.fast_forward_to_state(
            behaviour=self.price_estimation_behaviour,
            state_id=SignatureBehaviour.state_id,
            period_state=PeriodState(most_voted_tx_hash="68656c6c6f776f726c64"),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == SignatureBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_signing_request(
            request_kwargs=dict(
                performative=SigningMessage.Performative.SIGN_MESSAGE,
            ),
            response_kwargs=dict(
                performative=SigningMessage.Performative.SIGNED_MESSAGE,
                signed_message=SignedMessage(
                    ledger_id="ethereum", body="stub_signature"
                ),
            ),
        )
        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == FinalizeBehaviour.state_id


class TestFinalizeBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test FinalizeBehaviour."""

    def test_non_sender_act(
        self,
    ) -> None:
        """Test finalize behaviour."""
        participants = frozenset({self.skill.skill_context.agent_address, "a_1", "a_2"})
        self.fast_forward_to_state(
            behaviour=self.price_estimation_behaviour,
            state_id=FinalizeBehaviour.state_id,
            period_state=PeriodState(
                most_voted_keeper_address="most_voted_keeper_address",
                participants=participants,
            ),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == FinalizeBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == ValidateTransactionBehaviour.state_id

    def test_sender_act(
        self,
    ) -> None:
        """Test finalize behaviour."""
        participants = frozenset({self.skill.skill_context.agent_address, "a_1", "a_2"})
        self.fast_forward_to_state(
            behaviour=self.price_estimation_behaviour,
            state_id=FinalizeBehaviour.state_id,
            period_state=PeriodState(
                most_voted_keeper_address=self.skill.skill_context.agent_address,
                safe_contract_address="safe_contract_address",
                participants=participants,
                estimate=1.0,
                participant_to_signature={},
                most_voted_estimate=1.0,
            ),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == FinalizeBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_contract_api_request(
            request_kwargs=dict(
                performative=ContractApiMessage.Performative.GET_RAW_TRANSACTION,
            ),
            response_kwargs=dict(
                performative=ContractApiMessage.Performative.RAW_TRANSACTION,
                callable="get_deploy_transaction",
                raw_transaction=RawTransaction(
                    ledger_id="ethereum", body={"tx_hash": "0x3b"}
                ),
            ),
        )
        self.mock_signing_request(
            request_kwargs=dict(
                performative=SigningMessage.Performative.SIGN_TRANSACTION
            ),
            response_kwargs=dict(
                performative=SigningMessage.Performative.SIGNED_TRANSACTION,
                signed_transaction=SignedTransaction(ledger_id="ethereum", body={}),
            ),
        )
        self.mock_ledger_api_request(
            request_kwargs=dict(
                performative=LedgerApiMessage.Performative.SEND_SIGNED_TRANSACTION
            ),
            response_kwargs=dict(
                performative=LedgerApiMessage.Performative.TRANSACTION_DIGEST,
                transaction_digest=TransactionDigest(
                    ledger_id="ethereum", body="tx_hash"
                ),
            ),
        )
        self.mock_ledger_api_request(
            request_kwargs=dict(
                performative=LedgerApiMessage.Performative.GET_TRANSACTION_RECEIPT
            ),
            response_kwargs=dict(
                performative=LedgerApiMessage.Performative.TRANSACTION_RECEIPT,
                transaction_receipt=TransactionReceipt(
                    ledger_id="ethereum", receipt={}, transaction={}
                ),
            ),
        )
        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == ValidateTransactionBehaviour.state_id


class TestValidateTransactionBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test ValidateTransactionBehaviour."""

    def test_validate_transaction_safe_behaviour(
        self,
    ) -> None:
        """Test ValidateTransactionBehaviour."""
        participants = frozenset({self.skill.skill_context.agent_address, "a_1", "a_2"})
        most_voted_keeper_address = self.skill.skill_context.agent_address
        self.fast_forward_to_state(
            behaviour=self.price_estimation_behaviour,
            state_id=ValidateTransactionBehaviour.state_id,
            period_state=PeriodState(
                safe_contract_address="safe_contract_address",
                final_tx_hash="final_tx_hash",
                participants=participants,
                most_voted_keeper_address=most_voted_keeper_address,
                most_voted_estimate=1.0,
                participant_to_signature={},
            ),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == ValidateTransactionBehaviour.state_id
        )
        self.price_estimation_behaviour.act_wrapper()
        self.mock_contract_api_request(
            request_kwargs=dict(performative=ContractApiMessage.Performative.GET_STATE),
            response_kwargs=dict(
                performative=ContractApiMessage.Performative.STATE,
                callable="get_deploy_transaction",
                state=TrState(ledger_id="ethereum", body={"verified": True}),
            ),
        )
        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == ResetBehaviour.state_id


class TestResetBehaviour(PriceEstimationFSMBehaviourBaseCase):
    """Test ResetBehaviour."""

    def test_end_behaviour(
        self,
    ) -> None:
        """Test end behaviour."""
        self.fast_forward_to_state(
            behaviour=self.price_estimation_behaviour,
            state_id=ResetBehaviour.state_id,
            period_state=PeriodState(
                most_voted_estimate=0.1,
                final_tx_hash="68656c6c6f776f726c64",
            ),
        )
        assert (
            cast(
                BaseState,
                cast(BaseState, self.price_estimation_behaviour.current_state),
            ).state_id
            == ResetBehaviour.state_id
        )
        self.price_estimation_behaviour.context.params.observation_interval = 0.1
        self.price_estimation_behaviour.act_wrapper()
        time.sleep(0.3)
        self.price_estimation_behaviour.act_wrapper()
        self.mock_a2a_transaction()
        self._test_done_flag_set()
        self.end_round()
        state = cast(BaseState, self.price_estimation_behaviour.current_state)
        assert state.state_id == RandomnessInOperationBehaviour.state_id
