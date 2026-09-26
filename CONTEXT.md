# tinyaisense

A hub that lets LLM agents perceive and act on the physical world through small IoT devices: hearing, speaking, seeing, displaying, and receiving human input.

## Language

### Physical side

**Device**:
A microcontroller node (e.g. an M5Stack Atom Echo) running tinyaisense firmware and hosting one or more capabilities. It is physical, or stood in for by the Simulator; the hub treats both the same.
_Avoid_: Node, sensor (for the whole unit), board

**Simulator**:
A PC program that stands in for one or more devices, speaking the same protocol and behaving as their firmware would. A device it runs is a **simulated device**.
_Avoid_: Emulator, fake device, mock

**Board**:
The hardware model a device is built on (e.g. `m5stack-atom-echo`). It fixes which capabilities the device has and how their hardware behaves. Firmware is built for one board.
_Avoid_: Model, platform, hardware type; don't call a single device a board

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

**Session**:
One agent's claim on the hub, from when it claims the hub until it drops, is taken over, or goes idle. The hub allows one at a time. The agent's streams, LED claim and Reflex settings belong to it and end with it. One agent may reach the hub over several links (e.g. the agent API plus MCP) within the same session.
_Avoid_: Connection (reserved for devices), client

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

**Status**:
What a device shows the human about itself, the hub and the agent (e.g. offline, pending, listening, thinking), on whatever indicator its board has. An agent's LED claim suspends it, except for listening, which always shows.
_Avoid_: LED state, indicator, mode

**Utterance**:
One span of human speech captured from a mic and delivered to the agent as a unit.
_Avoid_: Recording, clip, turn

**Voice**:
A named way the hub speaks text aloud in one language, which an agent picks by id. It hides which speech engine, local or cloud, produces the audio.
_Avoid_: Speaker (that's the actuator), TTS model, engine

**Interrupt**:
Stopping an agent's running turn because of an Event (e.g. a button press). An adapter running next to the agent does it; the hub only delivers the Event.
_Avoid_: Barge-in (that's the human cutting off speech), cancel

**Barge-in**:
The human cutting off the agent's speech. On the MVP this is done by the button, via a Reflex.
_Avoid_: Interrupt (reserved for the hub interrupting an agent's loop)
