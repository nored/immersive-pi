/* Diagnostic wrappers: measure GL driver calls before returning to Python. */
#include <stdint.h>
#include <time.h>

typedef void (*texture_fn)(unsigned, int, int, int, unsigned, unsigned,
                           unsigned, unsigned, const void *);
typedef void (*finish_fn)(void);
static texture_fn texture;
static finish_fn finish;
uint64_t texture_wall_ns, texture_cpu_ns, finish_wall_ns, finish_cpu_ns;

static uint64_t now(clockid_t clock)
{
    struct timespec stamp;
    clock_gettime(clock, &stamp);
    return (uint64_t)stamp.tv_sec * 1000000000ULL + stamp.tv_nsec;
}

void set_functions(void *texture_address, void *finish_address)
{
    texture = (texture_fn)texture_address;
    finish = (finish_fn)finish_address;
}

void timed_texture(unsigned target, int level, int x, int y,
                   unsigned width, unsigned height, unsigned format,
                   unsigned type, const void *pixels)
{
    uint64_t wall = now(CLOCK_MONOTONIC), cpu = now(CLOCK_THREAD_CPUTIME_ID);
    texture(target, level, x, y, width, height, format, type, pixels);
    texture_cpu_ns = now(CLOCK_THREAD_CPUTIME_ID) - cpu;
    texture_wall_ns = now(CLOCK_MONOTONIC) - wall;
}

void timed_finish(void)
{
    uint64_t wall = now(CLOCK_MONOTONIC), cpu = now(CLOCK_THREAD_CPUTIME_ID);
    finish();
    finish_cpu_ns = now(CLOCK_THREAD_CPUTIME_ID) - cpu;
    finish_wall_ns = now(CLOCK_MONOTONIC) - wall;
}
