#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <pthread.h>
#include "ladder.h"

void initializeHardware() {}
void finalizeHardware() {}
void updateBuffersOut() {}

void updateBuffersIn()
{
    const char *path = std::getenv("SWU_SENSOR_FILE");
    if (!path) path = "/sensors/inputs.bin";
    FILE *file = std::fopen(path, "rb");
    if (!file) {
        std::perror("OpenPLC sensor input");
        std::exit(EXIT_FAILURE);
    }
    unsigned char bytes[6];
    const size_t count = std::fread(bytes, 1, sizeof(bytes), file);
    std::fclose(file);
    if (count != sizeof(bytes)) {
        std::fputs("Invalid OpenPLC sensor input frame\n", stderr);
        std::exit(EXIT_FAILURE);
    }
    pthread_mutex_lock(&bufferLock);
    for (int i = 0; i < 3; ++i) {
        if (int_input[i]) *int_input[i] = static_cast<uint16_t>((bytes[2*i] << 8) | bytes[2*i+1]);
    }
    pthread_mutex_unlock(&bufferLock);
}
