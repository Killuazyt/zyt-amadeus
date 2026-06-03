export default class AnimationControl {
  head_move: number
  head: number[]
  target_head: number[]
  head_min: number[]
  head_max: number[]
  head_speed: number[]
  eye_lids: number[]
  blink_time: number
  blink_speed: number[]
  eye_move: number
  eyes: number[]
  target_eyes: number[]
  eyes_min: number[]
  eyes_max: number[]
  eyes_speed: number[]
  eye_lids_state: string
  blink_state: string
  blink_length: number

  constructor() {
    this.head_move = 0
    this.head = [0, 0, 0]
    this.target_head = [0, 0, 0]
    this.head_min = [-12, -12, -12]
    this.head_max = [12, 12, 12]
    this.head_speed = [1000, 2000]

    this.eye_move = 0
    this.eyes = [0, 0]
    this.target_eyes = [-20, 20]
    this.eyes_min = [-0.5, -0.5]
    this.eyes_max = [0.5, 0.5]
    this.eyes_speed = [1000, 2000]

    this.blink_time = 0
    this.blink_speed = [5000, 5000]
    this.eye_lids = [1, 1]
    this.eye_lids_state = 'Blink'
    this.blink_state = 'Open'
    this.blink_length = 100
  }

  lerp(a: number, b: number, t: number): number {
    return a + (b - a) * t
  }

  head_movement(current_time: number): void {
    if (current_time > this.head_move) {
      this.head_move = current_time + Math.random() * (this.head_speed[1] - this.head_speed[0]) + this.head_speed[0]
      this.target_head[0] = Math.random() * (this.head_max[0] - this.head_min[0]) + this.head_min[0]
      this.target_head[1] = Math.random() * (this.head_max[1] - this.head_min[1]) + this.head_min[1]
      this.target_head[2] = Math.random() * (this.head_max[2] - this.head_min[2]) + this.head_min[2]
    }
    this.head[0] = this.lerp(this.head[0], this.target_head[0], 0.1)
    this.head[1] = this.lerp(this.head[1], this.target_head[1], 0.1)
    this.head[2] = this.lerp(this.head[2], this.target_head[2], 0.1)
  }

  eyes_movement(current_time: number): void {
    if (current_time > this.eye_move) {
      this.eye_move = current_time + Math.random() * (this.eyes_speed[1] - this.eyes_speed[0]) + this.eyes_speed[0]
      this.target_eyes[0] = Math.random() * (this.eyes_max[0] - this.eyes_min[0]) + this.eyes_min[0]
      this.target_eyes[1] = Math.random() * (this.eyes_max[1] - this.eyes_min[1]) + this.eyes_min[1]
    }
    this.eyes[0] = this.lerp(this.eyes[0], this.target_eyes[0], 0.1)
    this.eyes[1] = this.lerp(this.eyes[1], this.target_eyes[1], 0.1)
  }

  eyes_lid_movement(current_time: number): void {
    if (this.eye_lids_state === 'Blink') {
      if (current_time > this.blink_time && this.blink_state === 'Blink') {
        this.blink_time = current_time + Math.random() * (this.blink_speed[1] - this.blink_speed[0]) + this.blink_speed[0]
        this.eye_lids = [1, 1]
        this.blink_state = 'Open'
      } else if (current_time > this.blink_time) {
        this.blink_time = current_time + this.blink_length
        this.eye_lids = [0, 0]
        this.blink_state = 'Blink'
      }
    }
  }
}
