import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', component: () => import('../views/Login.vue'), meta: { public: true } },
  {
    path: '/',
    component: () => import('../views/Layout.vue'),
    children: [
      { path: '', redirect: '/dashboard' },
      { path: 'dashboard', component: () => import('../views/Dashboard.vue'), meta: { title: '仪表盘' } },
      { path: 'signals', component: () => import('../views/SignalLog.vue'), meta: { title: '信号日志' } },
      { path: 'orders', component: () => import('../views/Orders.vue'), meta: { title: '订单持仓' } },
      { path: 'strategies', component: () => import('../views/Strategies.vue'), meta: { title: '策略管理' } },
      { path: 'screener', component: () => import('../views/Screener.vue'), meta: { title: '选股器' } },
      { path: 'backtest', component: () => import('../views/Backtest.vue'), meta: { title: '回测中心' } },
      { path: 'risk', component: () => import('../views/RiskSettings.vue'), meta: { title: '风控设置' } },
      { path: 'notify', component: () => import('../views/NotifySettings.vue'), meta: { title: '通知设置' } },
      { path: 'settings', component: () => import('../views/Settings.vue'), meta: { title: '系统设置' } },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  if (!to.meta.public && !localStorage.getItem('token')) return '/login'
})

export default router
