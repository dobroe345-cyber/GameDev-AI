#include "engine.h"

#include <algorithm>
#include <vector>

namespace {

class World {
public:
    std::uint32_t spawn(float x, float y, float vx, float vy) {
        gdai::Entity e;
        e.id = ++next_id_;
        e.pos = {x, y};
        e.vel = {vx, vy};
        e.alive = true;
        entities_.push_back(e);
        return e.id;
    }

    void kill(std::uint32_t id) {
        if (auto* e = find(id)) e->alive = false;
    }

    void step(float dt) {
        for (auto& e : entities_) {
            if (!e.alive) continue;
            e.pos.x += e.vel.x * dt;
            e.pos.y += e.vel.y * dt;
        }
        entities_.erase(
            std::remove_if(entities_.begin(), entities_.end(),
                           [](const gdai::Entity& e) { return !e.alive; }),
            entities_.end());
    }

    gdai::Entity* find(std::uint32_t id) {
        for (auto& e : entities_)
            if (e.id == id && e.alive) return &e;
        return nullptr;
    }

    std::uint32_t count() const { return static_cast<std::uint32_t>(entities_.size()); }

private:
    std::vector<gdai::Entity> entities_;
    std::uint32_t next_id_ = 0;
};

World* as_world(void* p) { return static_cast<World*>(p); }

}  // namespace

void* gdai_create(void) { return new World(); }

void gdai_destroy(void* world) { delete as_world(world); }

std::uint32_t gdai_spawn(void* world, float x, float y, float vx, float vy) {
    return as_world(world)->spawn(x, y, vx, vy);
}

void gdai_kill(void* world, std::uint32_t id) { as_world(world)->kill(id); }

void gdai_step(void* world, float dt) { as_world(world)->step(dt); }

int gdai_get_pos(void* world, std::uint32_t id, float* out_x, float* out_y) {
    const gdai::Entity* e = as_world(world)->find(id);
    if (!e || !out_x || !out_y) return 0;
    *out_x = e->pos.x;
    *out_y = e->pos.y;
    return 1;
}

std::uint32_t gdai_count(void* world) { return as_world(world)->count(); }
