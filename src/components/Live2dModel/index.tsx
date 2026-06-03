import { useEffect, useRef, useCallback } from 'react'
import { Live2DModel, MotionPriority } from 'pixi-live2d-display'
import AnimationControl from './AnimationControl'
import { roleToLive2dMapper } from '@/constants/live2d'

const RANDOM_MOTIONS = ['random1', 'random2', 'random3', 'random4', 'random5']

interface Props {
  role: string
  emotion?: string | null
  motion?: string | null
  onModelReady?: () => void
}

export default function Live2dCanvas({ role, emotion, motion, onModelReady }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const modelRef = useRef<Live2DModel | null>(null)
  const movementRef = useRef(new AnimationControl())

  const animate = useCallback((currentTime: number) => {
    const model = modelRef.current
    const m = movementRef.current
    if (!model) return

    m.head_movement(currentTime)
    m.eyes_movement(currentTime)
    m.eyes_lid_movement(currentTime)

    const params: [string, number][] = [
      ['ParamAngleX', m.head[0]],
      ['ParamAngleY', m.head[1]],
      ['ParamAngleZ', m.head[2]],
      ['ParamBodyAngleX', m.head[0] / 10],
      ['ParamBodyAngleY', m.head[1] / 10],
      ['ParamBodyAngleZ', m.head[2] / 10],
      ['ParamEyeBallX', m.eyes[0]],
      ['ParamEyeBallY', m.eyes[1]],
      ['ParamEyeLOpen', m.eye_lids[0]],
      ['ParamEyeROpen', m.eye_lids[1]],
    ]

    params.forEach(([param, value]) => {
      model.internalModel.coreModel.setParameterValueById(param, value)
    })

    requestAnimationFrame(() => animate(Date.now()))
  }, [])

  // Load model
  useEffect(() => {
    if (!canvasRef.current || !role) return

    const roleConfig = roleToLive2dMapper[role]
    if (!roleConfig) return

    const app = new PIXI.Application({
      view: canvasRef.current,
      autoStart: true,
      transparent: true,
      resize: true,
      resizeTo: window,
    } as any)

    let destroyed = false

    Live2DModel.from(roleConfig.path, { autoInteract: false }).then((model) => {
      if (destroyed) return

      modelRef.current = model
      // @ts-ignore - pixi-live2d-display model compatible with PIXI stage
      app.stage.addChild(model)

      // Position
      model.scale.set(roleConfig.scale1)
      model.x = roleConfig.x1 + (window.innerWidth - 1620) / 2
      model.y = roleConfig.y1

      // Disable built-in breath/motion blending
      // @ts-ignore - pixi-live2d-display extends PIXI with live2d namespace
      PIXI.live2d.config.motionFadingDuration = 0
      // @ts-ignore
      PIXI.live2d.config.idleMotionFadingDuration = 0
      // @ts-ignore
      PIXI.live2d.config.expressionFadingDuration = 0
      model.internalModel.breath = null

      // Override motionManager.update to use Date.now()
      const updateFn = model.internalModel.motionManager.update
      model.internalModel.motionManager.update = () => {
        updateFn.call(model.internalModel.motionManager, model.internalModel.coreModel, Date.now() / 1000)
      }

      // Start idle animation
      requestAnimationFrame(() => animate(Date.now()))

      onModelReady?.()
    })

    const handleResize = () => {
      if (modelRef.current) {
        modelRef.current.scale.set(roleConfig.scale1)
        modelRef.current.x = roleConfig.x1 + (window.innerWidth - 1620) / 2
        modelRef.current.y = roleConfig.y1
      }
    }
    window.addEventListener('resize', handleResize)

    return () => {
      destroyed = true
      window.removeEventListener('resize', handleResize)
    }
  }, [role, animate, onModelReady])

  // Handle emotion changes
  useEffect(() => {
    const model = modelRef.current
    if (!model?.internalModel?.motionManager?.expressionManager) return

    if (!emotion || emotion === 'normal') {
      model.internalModel.motionManager.expressionManager.resetExpression()
    } else {
      model.internalModel.motionManager.expressionManager.setExpression(emotion)
    }
  }, [emotion])

  // Handle motion changes
  useEffect(() => {
    const model = modelRef.current
    if (!model?.internalModel?.motionManager) return

    if (motion === 'speaking' || motion === 'thinking') {
      const randomMotion = RANDOM_MOTIONS[Math.floor(Math.random() * RANDOM_MOTIONS.length)]
      model.internalModel.motionManager.startMotion(randomMotion, 0, MotionPriority.FORCE)
    } else if (motion) {
      model.internalModel.motionManager.startMotion(motion, 0, MotionPriority.FORCE)
    }
  }, [motion])

  return (
    <canvas
      ref={canvasRef}
      className="fixed inset-0 w-screen h-screen pointer-events-none z-[1]"
    />
  )
}
