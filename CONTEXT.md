# tinyaisense

A hub that lets LLM agents perceive and act on the physical world through small IoT devices: hearing, speaking, seeing, displaying, and receiving human input.

## Language

### Physical side

**Device**:
A physical microcontroller node (e.g. an M5Stack Atom Echo) running tinyaisense firmware and hosting one or more capabilities.
_Avoid_: Node, sensor (for the whole unit), board

**Capability**:
A single sensor or actuator on a device.
_Avoid_: Feature, peripheral

**Address**:
How an agent names a capability: the device's name plus the capability's id within that device (e.g. `kitchen/speaker`).
_Avoid_: Path, URI, handle

**Kind**:
The type of a capability (e.g. mic, speaker, button, LED). The kind fixes which events, streams and commands the capability has.
_Avoid_: Capability type, class

**Manifest**:
A device's self-description, sent when it connects: its identity and its capabilities. It is fixed for the life of the connection.
_Avoid_: Descriptor, profile

**Sensor**:
A capability that takes input from the environment or a human (mic, button, camera).
_Avoid_: Input device

**Actuator**:
A capability that produces output into the environment (speaker, LED, screen).
_Avoid_: Instrument, output device

### Service side

**Hub**:
The software service that connects to devices on one side and agents on the other. Contains no LLM of its own.
_Avoid_: Server, gateway, bridge

**Agent**:
An external LLM-driven program that consumes the hub, either as an MCP client or through the agent API.
_Avoid_: Client, bot, assistant

**Connection**:
One live link between a device and the hub, from hello to drop. All stream and command state belongs to it and dies with it. A device is **online** while it has one.
_Avoid_: Session (reserved for agents), link, channel

**Pairing**:
The owner approving a new device on the hub, which then issues the device its token. An unpaired device is **pending**.
_Avoid_: Enrollment, registration, adoption

### Data

**Event**:
A discrete signal from a capability at a point in time (e.g. a button press).
_Avoid_: Message, notification

**Stream**:
Continuous data flowing to or from a capability (e.g. mic audio, speaker audio).
_Avoid_: Feed, channel

**Command**:
A discrete request from the hub to a capability that always gets a reply (e.g. set the LED colour).
_Avoid_: Action, call, message

### Conversation

**Reflex**:
A reaction the hub arms on a device, which the device then carries out by itself when triggered (e.g. a button press cuts the speaker and opens the mic), without waiting for the hub or an agent.
_Avoid_: Trigger, binding, shortcut

**Utterance**:
One span of human speech captured from a mic and delivered to the agent as a unit.
_Avoid_: Recording, clip, turn

**Voice**:
A named way the hub speaks text aloud in one language, which an agent picks by id. It hides which speech engine, local or cloud, produces the audio.
_Avoid_: Speaker (that's the actuator), TTS model, engine

**Barge-in**:
The human cutting off the agent's speech. On the MVP this is done by the button, via a Reflex.
_Avoid_: Interrupt (reserved for the hub interrupting an agent's loop)
