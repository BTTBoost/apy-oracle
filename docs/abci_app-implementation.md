# ABCIApp implementation

## Reactiveness and proactiveness

ABCI apps have two different sides that make them fully autonomous and not just automatic: reactiveness and proactiveness. While the first one allows for the application to respond to external events, the second one is the one that makes possible to take decissions and actions on its own. These two aspects are implemented in different classes:

- `Handlers` implement AEAs' reactive behaviour. Each Skill has zero, one or more handler objects. There is a one-to-one correspondence between Handlers and the protocols in an AEA (also known as the registered protocols). If an AEA understands a `Protocol` referenced in a received `Envelope` (i.e. the protocol is registered in this AEA), this envelope is sent to the corresponding Handler which executes the AEA's reaction to this `Message`.

- `Behaviours` encapsulate actions which further the AEAs goal and are initiated by internals of the AEA rather than external events. Behaviours implement AEAs' pro-activeness. The Open AEA framework provides a number of abstract base classes implementing different types of simple and composite behaviours (e.g. cyclic, one-shot, finite-state-machine, etc), and these define how often and in what order a behaviour and its sub-behaviours must be executed. Behaviours are acting as a user in a traditional blockchain.

## The AbciApp class

The `ABCIApp` provides the necessary interface for implementation of ABCI-based
Finite State Machine applications. Implementation of the `AbciApp` requires the
developer to implement the class attributes `initial_round_cls`,
`transition_function` and `final_states` when creating concrete subclasses. The
`_MetaRoundBehaviour` metaclass is used to enforce this during implementation
by the developer.

```python
# skills.abstract_round_behaviour.base.py


AppState = Type[AbstractRound]
AbciAppTransitionFunction = Dict[AppState, Dict[EventType, AppState]]
EventToTimeout = Dict[EventType, float]


class AbciApp(
    Generic[EventType], ABC, metaclass=_MetaAbciApp
):
    """Base class for ABCI apps."""

    initial_round_cls: AppState
    initial_states: Set[AppState] = set()
    transition_function: AbciAppTransitionFunction
    final_states: Set[AppState] = set()
    event_to_timeout: EventToTimeout = {}

    def __init__(
        self,
        state: BasePeriodState,
        consensus_params: ConsensusParams,
    ):
        """Initialize the AbciApp."""

    def process_transaction(self, transaction: Transaction) -> None:
        """Process a transaction."""

    def process_event(self, event: EventType, result: Optional[Any] = None) -> None:
        """Process a round event."""

    def update_time(self, timestamp: datetime.datetime) -> None:
        """Observe timestamp from last block."""
    ...
```

Some of its methods relate to concepts discussed in the [FSM section](./fsm.md):

- `process_transaction` processes the payload generated by the agents during a round.
- `process_event` allows for the execution of transitions to the next round based on the output of the current round.
- `update_time` allows for resetting of timeouts based on the timestamp from last
  block. This is the only form of synchronization of time that exists in this
  system of asynchronously operating AEAs, an understanding of which is
  indispensable to a developer that needs to implement any sort of
  [time-based](https://valory-xyz.github.io/open-aea/agent-oriented-development/#time)
  logic as part of their agents' behaviour.


A concrete implementation of a subclass of `AbciApp` looks as follows:

```python
class MyAbciApp(AbciApp):
    """My ABCI-based Finite-State Machine Application execution behaviour"""

    initial_round_cls: AppState = RoundA
    initial_states: Set[AppState] = set()
    transition_function: AbciAppTransitionFunction = {
        RoundA: {
            Event.DONE: RoundB,
            Event.ROUND_TIMEOUT: RoundA,
            Event.NO_MAJORITY: RoundA,
        },
        RoundB: {
            Event.DONE: FinalRound,
            Event.ROUND_TIMEOUT: RoundA,
            Event.NO_MAJORITY: RoundA,
        },
        FinalRound: {},
    }
    final_states: Set[AppState] = {FinalRound}
    event_to_timeout: EventToTimeout = {
        Event.ROUND_TIMEOUT: 30.0,
    }
    ...
```

The `initial_states` are optionally provided by the developer, if none are
provided the `initial_round_cls` is inferred to be the initial state.
When we process an `Event` we schedule the next round, find the associated
next events from the `transition_function` and set the associated timeouts, if
any. Once the [Application BlockChain Interface](./abci.md) application is
implemented, the application requires `AbstractRoundBehaviour` to enact the
state transition logic contained in it.


### ABCIApp diagrams

These sequence diagrams show the sequence of messages and method calls between
the software components.

The following diagram describes the addition of transactions to the transaction
pool:

<div class="mermaid">
    sequenceDiagram
        participant ConsensusEngine
        participant ABCIHandler
        participant Period
        participant Round
        activate Round
        note over ConsensusEngine,ABCIHandler: client submits transaction tx
        ConsensusEngine->>ABCIHandler: RequestCheckTx(tx)
        ABCIHandler->>Period: check_tx(tx)
        Period->>Round: check_tx(tx)
        Round->>Period: OK
        Period->>ABCIHandler: OK
        ABCIHandler->>ConsensusEngine: ResponseCheckTx(tx)
        note over ConsensusEngine,ABCIHandler: tx is added to tx pool
</div>

The following diagram describes the delivery of transactions in a block:

<div class="mermaid">
    sequenceDiagram
        participant ConsensusEngine
        participant ABCIHandler
        participant Period
        participant Round1
        participant Round2
        activate Round1
        note over Round1,Round2: Round1 is the active round,<br/>Round2 is the next round
        note over ConsensusEngine,ABCIHandler: validated block ready to<br/>be submitted to the ABCI app
        ConsensusEngine->>ABCIHandler: RequestBeginBlock()
        ABCIHandler->>Period: begin_block()
        Period->>ABCIHandler: ResponseBeginBlock(OK)
        ABCIHandler->>ConsensusEngine: OK
        loop for tx_i in block
            ConsensusEngine->>ABCIHandler: RequestDeliverTx(tx_i)
            ABCIHandler->>Period: deliver_tx(tx_i)
            Period->>Round1: deliver_tx(tx_i)
            Round1->>Period: OK
            Period->>ABCIHandler: OK
            ABCIHandler->>ConsensusEngine: ResponseDeliverTx(OK)
        end
        ConsensusEngine->>ABCIHandler: RequestEndBlock()
        ABCIHandler->>Period: end_block()
        alt if condition is true
            note over Period,Round1: replace Round1 with Round2
            deactivate Round1
            Period->>Round2: schedule
            activate Round2
        end
        Period->>ABCIHandler: OK
        ABCIHandler->>ConsensusEngine: ResponseEndBlock(OK)
        deactivate Round2
</div>