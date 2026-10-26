# Memory 原子准入 M1 验证证据

- `acceptance-spec.json`：实际执行的15个受影响单测模块、基线工作树与虚拟环境参数。重跑时替换机器绝对路径，使用待验收checkout作为cwd及其项目Python。
- `acceptance-status.json`、`acceptance-stdout.log`、`acceptance-stderr.log`、`acceptance-junit.xml`：225通过、0失败、0跳过，真实进程退出码0。新原子准入36项、新运行时归因/touch10项均在这225项内，不重复相加。
- `runtime-first-junit.xml`：首次主线42项中有2个新夹具错误，输出预留大于测试小窗口，保留失败；最终测试显式设置合法预留，没有放宽生产准入。
- `atomic-admission-first.log`：独立测试首次36项中发现2个非法trust类型未失败关闭的真实缺口，随后修复；原断言保留。
- `static-checks.txt`：仓库级Ruff、改动文件格式与产品diff检查均退出码0。
- `atomic_projection_probe.py`：一个离线机制案例，不是质量数据集。用 `python atomic_projection_probe.py <checkout-root>` 分别在基线和修复checkout重放；结果见summary。

机制案例的完整事实数前后都是0：预算不够时，新版把不完整记录改为整条缺省。不能据此宣称召回、回答正确率或压缩语义质量提高。字符长度只是可解释的离线计数器，不代表供应商精确tokens。

没有新增业务模型API、数据库、迁移或在线发现请求；子智能体协作消耗单独存在，不冒充业务模型零总消耗。原工作区的方案草稿和学习文档不在本分支改写。
