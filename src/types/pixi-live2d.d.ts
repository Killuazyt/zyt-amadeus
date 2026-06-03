declare module 'pixi-live2d-display' {
  export class Live2DModel {
    internalModel: {
      motionManager: {
        expressionManager: {
          setExpression: (expression: string) => void
          resetExpression: () => void
        }
        startMotion: (motion: string, priority: number, priority_type: number) => void
        update: (coreModel: any, time: number) => void
      }
      coreModel: {
        setParameterValueById: (id: string, value: number) => void
      }
      breath: any
    }
    scale: {
      set: (scale: number) => void
    }
    x: number
    y: number

    static from(source: string, options?: any): Promise<Live2DModel>
  }

  export enum MotionPriority {
    NONE = 0,
    IDLE = 1,
    NORMAL = 2,
    FORCE = 3,
  }
}

declare namespace PIXI {
  export class Application {
    constructor(options: {
      view: HTMLCanvasElement
      autoStart: boolean
      transparent: boolean
      resize: boolean
      resizeTo: Window
    })
    stage: {
      addChild: (model: any) => void
    }
    resize: () => void
  }

  export namespace live2d {
    export const config: {
      motionFadingDuration: number
      idleMotionFadingDuration: number
      expressionFadingDuration: number
    }
  }
}
