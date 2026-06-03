interface Live2dModelConfig {
  path: string
  scale1: number
  scale2: number
  x1: number
  x2: number
  y1: number
  y2: number
}

export const roleToLive2dMapper: Record<string, Live2dModelConfig> = {
  牧濑红莉栖: {
    path: 'https://static.amadeus-web.top/live2dmodels/steinsGateKurisuNew/红莉栖.model3.json',
    scale1: 0.42,
    scale2: 0.42,
    x1: 550,
    x2: -100,
    y1: 50,
    y2: 20,
  },
}
