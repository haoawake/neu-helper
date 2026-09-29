"""Offline UI fixture. python tests/preview_today.py (synthetic data only)."""
import ast
from datetime import datetime, timedelta
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flask import Flask, jsonify, request, send_from_directory, Response
import today

app = Flask(__name__)
now = datetime.now().astimezone()
store = today.ActionStore(ROOT/'data'/'today_preview_actions.json')
memo_items = [dict(id='demo-memo', text='整理本周复习笔记', kind='plain', done=False, info={})]
canvas = {'todo':[dict(key='assignment:1', title='CS5800 · 算法作业：动态规划', due_utc=(now-timedelta(hours=2)).isoformat(), url='https://example.edu/assignments/1'),
                  dict(key='assignment:2', title='提交项目阶段报告', due_utc=now.replace(hour=23,minute=59).isoformat(),url='https://example.edu/assignments/2')]}
class PreviewBackend:
    today_actions = store
    def today_view(self):
        result = today.build(canvas=canvas, messages=[dict(id='notice',subject='图书馆开放时间调整',unread=True,ts=now.isoformat(),rank={'level':2})],
            flags={'follow':{'star':True,'meta':{'subject':'回复导师：确认项目讨论时间','web_url':'https://example.edu/mail'}}},
            memo_items=memo_items,schedule={'items':[dict(id='class',title='算法课',course='CS5800',weekday=(now.weekday()+1)%7,start='16:00')]},events=[],marks=store.all())
        result['sources']={'canvas':{'state':'error','last_success':(now-timedelta(hours=2)).isoformat(),'message':'演示：网络连接失败，保留上次数据'},
            'accounts':[{'label':'大学邮箱','ready':True,'last_sync':now.isoformat()}], 'mail':{}, 'schedule':{'parsed':[{'at':now.isoformat()}],'state':{}}}
        return result
    def refresh_today(self): return True
backend = PreviewBackend()
tree = ast.parse((ROOT/'server.py').read_text(encoding='utf-8'))
nodes = [n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('api_today','api_today_action','api_today_refresh')]
exec(compile(ast.Module(body=nodes,type_ignores=[]),'server.py','exec'),globals())

@app.get('/')
def index(): return send_from_directory(ROOT/'gui','index.html')
@app.get('/app.js')
def script():
    src=(ROOT/'gui'/'app.js').read_text(encoding='utf-8')
    src=src.replace('function safeBoot() {', '''function safeBoot() {
      state.prefs = {lang: new URLSearchParams(location.search).get('lang') || 'zh'};
      document.documentElement.dataset.theme = new URLSearchParams(location.search).get('theme') || 'light';
      wireEvents(); switchTab('memo'); loadMemos();
      setPage(new URLSearchParams(location.search).get('page') || 'today');
      $('appbarMeta').textContent = '离线演示 · 合成数据';
      return;
    ''')
    return Response(src,mimetype='application/javascript')
@app.route('/api/<path:name>',methods=['GET','POST'])
def fixture(name):
    if name == 'memos': return jsonify(items=memo_items,pending=1)
    if name == 'clientlog': print('BROWSER ERROR',request.get_json(),flush=True)
    if name == 'prefs': return jsonify(lang='zh',page='today',mode='full')
    if name == 'mail/one': return jsonify(message={'id':'notice','subject':'图书馆开放时间调整','snippet':'演示邮件','from':'Library','rank':{'level':2},'files':[]})
    if name == 'schedule': return jsonify(items=[],allday=[],courses=[],state={},notes=[],parsed=[])
    return jsonify({})
@app.get('/<path:name>')
def assets(name): return send_from_directory(ROOT/'gui',name)
if __name__ == '__main__': app.run(host='127.0.0.1',port=8766,debug=False)

