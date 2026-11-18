"""List shipped scripts, callable locations and CLI options without importing analysis modules."""
import argparse,ast,json
from pathlib import Path

def catalog(folder=None,public_only=False):
    folder=Path(folder) if folder else Path(__file__).resolve().parent;entries=[]
    for path in sorted(folder.iterdir()):
        if path.suffix=='.py':
            tree=ast.parse(path.read_text(encoding='utf-8'));doc=ast.get_docstring(tree) or ''
            functions=[{'name':n.name,'line':n.lineno} for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
            main=any(isinstance(n,ast.If) and '__name__' in ast.unparse(n.test) for n in tree.body)
            options=sorted({arg.value for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='add_argument' for arg in node.args if isinstance(arg,ast.Constant) and isinstance(arg.value,str) and arg.value.startswith('--')})
            if public_only and not main:continue
            entries.append({'file':path.name,'type':'python','summary':doc.split('\n')[0],'cli':main,'options':options,'callables':functions})
        elif not public_only and (path.suffix=='.html' or path.name=='requirements.txt'):
            entries.append({'file':path.name,'type':'template' if path.suffix=='.html' else 'dependencies'})
    return {'schema_version':1,'feature_directory':str(folder.parent),'scripts':entries}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--public-only',action='store_true');p.add_argument('--output',type=Path);a=p.parse_args();text=json.dumps(catalog(public_only=a.public_only),ensure_ascii=False,indent=2)+'\n'
    if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text,encoding='utf-8')
    else:print(text,end='')
