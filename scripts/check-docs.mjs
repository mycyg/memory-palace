import {readFileSync,existsSync} from 'node:fs';
for(const language of ['README.md','README.en.md','README.ja.md']){
 const text=readFileSync(language,'utf8');
 for(const value of ['1.0','10','64','docs/diagrams/overview'])if(!text.includes(value))throw Error(`${language}: missing ${value}`);
}
for(const name of ['overview','write-correct','recall-context','background','proactive-contact'])for(const ext of ['mmd','svg','png']){
 const file=`docs/diagrams/${name}.${ext}`;
 if(!existsSync(file)||readFileSync(file).length<100)throw Error(`Missing diagram ${file}`);
}
console.log('README editions and all editable/rendered diagrams present');
