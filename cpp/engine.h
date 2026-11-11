// GameDev AI — минимальное ядро игрового движка (заготовка).
// Собирается в общую библиотеку и подключается из Python через ctypes.
#pragma once

#include <cstdint>

#ifdef _WIN32
  #define GDAI_API extern "C" __declspec(dllexport)
#else
  #define GDAI_API extern "C" __attribute__((visibility("default")))
#endif

namespace gdai {

struct Vec2 {
    float x = 0.f;
    float y = 0.f;
};

struct Entity {
    std::uint32_t id = 0;
    Vec2 pos;
    Vec2 vel;
    bool alive = false;
};

}  // namespace gdai

// C-интерфейс (стабильный ABI для ctypes / FFI)
GDAI_API void*         gdai_create(void);
GDAI_API void          gdai_destroy(void* world);
GDAI_API std::uint32_t gdai_spawn(void* world, float x, float y, float vx, float vy);
GDAI_API void          gdai_kill(void* world, std::uint32_t id);
GDAI_API void          gdai_step(void* world, float dt);
GDAI_API int           gdai_get_pos(void* world, std::uint32_t id, float* out_x, float* out_y);
GDAI_API std::uint32_t gdai_count(void* world);
