/*
 * SPDX-License-Identifier: MIT
 *
 * Copyright (C) 2013-2024 OpenMV, LLC.
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 *
 * Framebuffer functions.
 */
#ifndef __FRAMEBUFFER_H__
#define __FRAMEBUFFER_H__
#include <stdint.h>
#include "imlib.h"
#include "omv_common.h"
#include "common/queue.h"
#include "common/mutex.h"

#ifndef FRAMEBUFFER_ALIGNMENT
#define FRAMEBUFFER_ALIGNMENT    OMV_CACHE_LINE_SIZE
#endif

// If FB_FLAG_CHECK_LAST is set and this is the last buffer in
// the free queue the release logic depends on the buffer mode:
//
// - Single/FIFO: The buffer is released.
// - Double buffer: The buffer is not released.
// - Triple buffer: The last used buffer is released first.
typedef enum {
    FB_FLAG_NONE        = (1 << 0), // No special flags.
    FB_FLAG_USED        = (1 << 1), // Acquire from used / Release to free.
    FB_FLAG_FREE        = (1 << 2), // Acquire from free / Release to used.
    FB_FLAG_PEEK        = (1 << 3), // Acquire a buffer and keep it in the queue.
    FB_FLAG_CHECK_LAST  = (1 << 6), // Check if last buffer before releasing.
    FB_FLAG_INVALIDATE  = (1 << 7), // Invalidate buffer when acquired/released.
} framebuffer_flags_t;

// The frame buffer memory is used for the following:
//
// - Buffer queues: If the number of video buffers exceeds 3.
// - Video buffers: Consisting of a header followed by the buffer.
// - Unused memory: Available for buffer expansion or fb_alloc.
// - fb_alloc memory: Only for statically allocated frame buffers.
//
//              Dynamic Frame Buffer Memory Layout
// raw_base      pool_start               pool_end        raw_end
// ▼             ▼                        ▼                     ▼
// ┌────────────────────────────────────────────────────────────┐
// │ Queues¹ |    Frame Buffers Memory    |  Unused FB Memory²  │
// └────────────────────────────────────────────────────────────┘
//
// For static frame buffers, fb_alloc uses a fixed end region and
// may use the free space for transient allocations if available.
//
//              Static Frame Buffer Memory Layout
// fb_start  pool_start  pool_end   fb_alloc_sp      fb_alloc_end
// ▼         ▼           ▼          ▼                           ▼
// ┌────────────────────────────────────────────────────────────┐
// │ Queues¹ |  Buffers  | Unused FB Memory² |  Fixed FB Alloc  │
// └────────────────────────────────────────────────────────────┘
// ¹ Queues use frame buffer memory only if count > 3, otherwise
//   they're statically allocated to keep small buffers in SRAM.
//
// ² Unused frame buffer space can be used to expand buffers up
//   to the maximum available size (raw size minus queue size).
typedef struct framebuffer {
    int32_t x, y, w, h, u, v;
    PIXFORMAT_STRUCT;
    bool dynamic;       // Dynamically allocated or not.
    bool expanded;      // True if buffers were expanded.
    size_t raw_size;    // Raw buffer size and address.
    char *raw_base;
    size_t buf_size;    // Buffers size and count
    size_t buf_count;
    size_t frame_size;  // Actual frame size
    queue_t *used_queue;
    queue_t *free_queue;
    // Static memory for small queues.
    char raw_static[queue_calc_size(3) * 2];
} framebuffer_t;

// Drivers can add more flags:
// VB_FLAG_EXAMPLE1  (VB_FLAG_LAST << 0)
// VB_FLAG_EXAMPLE2  (VB_FLAG_LAST << 1)
typedef enum {
    VB_FLAG_NONE        = (1 << 0),
    VB_FLAG_USED        = (1 << 1),
    VB_FLAG_OVERFLOW    = (1 << 2),
    VB_FLAG_LAST        = (1 << 3),
} vbuffer_flags_t;

typedef struct vbuffer {
    int32_t offset;     // Write offset into the buffer (used by some drivers).
    uint32_t flags;     // Flags, see above.
    OMV_ATTR_ALIGNED(uint8_t data[], FRAMEBUFFER_ALIGNMENT);    // Data.
} vbuffer_t;

typedef struct jpegbuffer {
    int32_t w, h;
    int32_t size;
    int32_t enabled;
    int32_t quality;
    uint8_t *pixels;
    mutex_t lock;
} jpegbuffer_t;

void framebuffer_init0();

// Initializes a frame buffer instance.
void framebuffer_init(framebuffer_t *fb, void *buff, size_t size, bool dynamic);

// Initializes an image from the frame buffer.
void framebuffer_init_image(framebuffer_t *fb, image_t *img);

// Sets the frame buffer from an image.
void framebuffer_init_from_image(framebuffer_t *fb, image_t *img);

// Return the static frame buffer instance.
framebuffer_t *framebuffer_get(size_t id);

// Returns a pointer to the end of the framebuffer(s).
char *framebuffer_pool_end(framebuffer_t *fb);

// Clear the framebuffer FIFO.
void framebuffer_flush(framebuffer_t *fb);

// Change the number of buffers in the frame buffer.
// If expand is true, the buffer size will expand to use all of the
// available memory, otherwise it will equal the current frame size.
int framebuffer_resize(framebuffer_t *fb, size_t count, bool expand);

// Return true if free queue is not empty.
bool framebuffer_writable(framebuffer_t *fb);

// Return true if used queue is not empty.
bool framebuffer_readable(framebuffer_t *fb);

// FB_FLAG_USED: acquire buffer from used queue.
// FB_FLAG_FREE: acquire buffer from free queue.
vbuffer_t *framebuffer_acquire(framebuffer_t *fb, uint32_t flags);

// FB_FLAG_USED: release buffer from used queue.
// FB_FLAG_FREE: release buffer from free queue.
// Note: Returns NULL if the buffer was Not released.
vbuffer_t *framebuffer_release(framebuffer_t *fb, uint32_t flags);

// Reset a vbuffer state.
static inline void framebuffer_reset(vbuffer_t *buffer) {
    memset(buffer, 0, offsetof(vbuffer_t, data));
}

// Compress src image to the JPEG buffer if src is mutable,
// otherwise copy src to the JPEG buffer.
void framebuffer_update_jpeg_buffer(image_t *src);

// Use this macro to get a pointer to the JPEG buffer.
extern jpegbuffer_t jpegbuffer;
#define JPEG_FB()   (&jpegbuffer)

#endif /* __FRAMEBUFFER_H__ */
