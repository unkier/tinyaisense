// tinyaisense firmware. Layers, top down: the core (tas_core), the Kind modules (tas_kinds) and
// the Board (tas_board), with the pure-C audio code in tas_audio.

#include "tas_board.h"
#include "tas_core.h"

void app_main(void) {
    tas_core_start(tas_board());
}
